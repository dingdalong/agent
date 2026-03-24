import asyncio
import json
import logging
from typing import List, Dict, Any, Optional

from src.core.async_api import call_model
from src.plan.models import Plan, Step
from src.plan.exceptions import JSONParseError, APIGenerationError, PlanError
from src.tools import ToolDict
from config import PLAN_GENERATION_TIMEOUT

logger = logging.getLogger(__name__)


# === submit_plan 虚拟工具 schema ===

def build_submit_plan_schema() -> dict:
    """构建 submit_plan 虚拟工具的 schema，让 LLM 通过 function calling 提交计划"""
    step_schema = Step.model_json_schema()
    return {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "提交执行计划。每个步骤的 tool_name 和 tool_args 必须与可用工具的参数完全匹配。如果请求是简单问答或闲聊，不要调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "计划步骤列表",
                        "items": step_schema
                    }
                },
                "required": ["steps"]
            }
        }
    }


_SUBMIT_PLAN_SCHEMA = build_submit_plan_schema()


def _build_plan_tools(available_tools: List[ToolDict]) -> list:
    """构建计划生成用的工具列表：真实工具 + submit_plan"""
    return list(available_tools) + [_SUBMIT_PLAN_SCHEMA]


# === Prompt 模板 ===

CLARIFICATION_SYSTEM_PROMPT = """你是一个任务规划助手。在制定计划之前，你需要判断用户的请求是否提供了足够的信息来生成一个具体可执行的计划。

请根据"用户原始请求"和"已收集的信息"，判断当前掌握的信息是否足够生成一个具体、可执行的计划。

输出一个 JSON 对象：
- 如果信息已经充足，返回：{{"status": "ready"}}
- 如果还缺少关键信息，返回：{{"status": "need_info", "questions": "你要问用户的问题"}}

判断标准：
- 对照用户的原始请求，思考要完成这件事需要哪些关键信息（如时间、地点、人数、预算、偏好、约束条件等）
- 检查"已收集的信息"中是否已经覆盖了这些关键要素
- 如果已收集的信息已经涵盖了主要要素，即使不完美也应该返回 ready，不要过度追问
- 只针对尚未回答的关键信息提问，绝对不要重复问已经回答过的问题
- 提问要自然友好，像朋友对话一样，每次聚焦 2-3 个问题"""

PLAN_GENERATION_SYSTEM_PROMPT = """你是一个任务规划助手。根据用户请求，判断是否需要生成多步骤计划。

规则：
- 如果请求是简单的问答、闲聊或单步操作，直接回复文本，不要调用 submit_plan
- 如果请求需要多个步骤协作完成，调用 submit_plan 工具提交计划
- 每个步骤的 tool_name 和 tool_args 必须严格匹配可用工具的定义
- action 为 "subtask" 的步骤用于需要 LLM 生成文本内容的任务（如撰写文案、制定方案），subtask_prompt 要详细描述需要生成的内容
- 步骤之间通过 depends_on 声明依赖关系"""

PLAN_ADJUSTMENT_SYSTEM_PROMPT = """你是一个任务规划助手。用户对当前计划提出了修改意见，请根据意见调整计划，然后调用 submit_plan 提交调整后的计划。

规则：
- 如果用户要求删除某个步骤，则从列表中移除
- 如果要求添加步骤，请合理插入
- 如果要求修改步骤，请更新相应步骤
- tool_name 和 tool_args 必须严格匹配可用工具的定义"""

CONFIRM_CLASSIFICATION_PROMPT = """判断用户的回复是"确认执行计划"还是"要求调整计划"。

当前计划：
{plan_summary}

用户回复：{user_feedback}

请只输出一个JSON对象，格式为：{{"action": "confirm"}} 或 {{"action": "adjust"}}
- 如果用户表示同意、确认、执行、没问题等意思，输出 confirm
- 如果用户提出修改意见、补充要求、质疑等，输出 adjust"""


# === 解析函数 ===

def parse_plan_from_tool_calls(tool_calls: Dict[int, Dict[str, str]]) -> Optional[Plan]:
    """从 tool_calls 中解析 submit_plan 调用，返回 Plan 对象。
    如果没有 submit_plan 调用，返回 None。"""
    for _, tc in tool_calls.items():
        if tc.get("name") == "submit_plan":
            try:
                data = json.loads(tc["arguments"])
                steps = [Step(**s) for s in data.get("steps", [])]
                return Plan(steps=steps)
            except json.JSONDecodeError as e:
                raise JSONParseError(
                    f"submit_plan 参数 JSON 解析失败: {e}",
                    raw_response=tc["arguments"]
                ) from e
            except Exception as e:
                raise JSONParseError(
                    f"submit_plan 参数解析失败: {e}",
                    raw_response=tc["arguments"]
                ) from e
    return None


def parse_plan_response(response: str) -> Plan:
    """从纯文本 JSON 响应解析计划（兼容旧逻辑）"""
    try:
        data = json.loads(response)
        steps = [Step(**s) for s in data.get("steps", [])]
        return Plan(steps=steps)
    except json.JSONDecodeError as e:
        error_msg = f"JSON解析失败: {e}"
        logger.error(f"{error_msg}, 原始响应: {response}")
        raise JSONParseError(error_msg, raw_response=response) from e
    except Exception as e:
        error_msg = f"解析计划失败: {e}"
        logger.error(f"{error_msg}, 原始响应: {response}")
        raise JSONParseError(error_msg, raw_response=response) from e


# === 核心功能 ===

async def classify_user_feedback(user_feedback: str, plan: Plan) -> str:
    """使用LLM判断用户反馈是确认执行还是要求调整计划"""
    plan_summary = "\n".join(
        f"{i}. {step.description}" for i, step in enumerate(plan.steps, 1)
    )
    prompt = CONFIRM_CLASSIFICATION_PROMPT.format(
        plan_summary=plan_summary, user_feedback=user_feedback
    )

    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "user", "content": prompt}
            ], stream=False, temperature=0,
            response_format={"type": "json_object"}),
            timeout=15
        )
        data = json.loads(response)
        action = data.get("action", "adjust")
        if action in ("confirm", "adjust"):
            return action
        return "adjust"
    except Exception as e:
        logger.warning(f"分类用户反馈失败: {e}, 默认为调整")
        return "adjust"


async def check_clarification_needed(user_input: str, gathered_info: str = "") -> Optional[str]:
    """判断用户请求是否信息充足，不足则返回要问的问题，充足返回 None。"""
    user_message = f"用户原始请求：{user_input}\n\n已收集的信息：\n{gathered_info or '（暂无）'}"

    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": CLARIFICATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ], stream=False, temperature=0,
            response_format={"type": "json_object"}),
            timeout=15
        )
        data = json.loads(response)
        if data.get("status") == "need_info":
            return data.get("questions", "请提供更多细节。")
        return None
    except Exception as e:
        logger.warning(f"信息充分性判断失败: {e}，跳过信息收集")
        return None


async def generate_plan(user_input: str, available_tools: List[ToolDict], context: str = "") -> Optional[Plan]:
    """生成初始计划。使用 function calling 让 LLM 通过 submit_plan 工具提交计划。
    如果模型判断不需要计划（不调用 submit_plan），返回 None。"""
    plan_tools = _build_plan_tools(available_tools)
    user_prompt = f"用户请求：{user_input}\n{context}"

    try:
        content, tool_calls, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": PLAN_GENERATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ], stream=False, tools=plan_tools),
            timeout=PLAN_GENERATION_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise APIGenerationError(
            f"计划生成超时({PLAN_GENERATION_TIMEOUT}秒)", api_error=e
        ) from e
    except Exception as e:
        raise APIGenerationError(f"API调用失败: {e}", api_error=e) from e

    # LLM 没调用 submit_plan → 不需要计划
    if not tool_calls:
        return None

    plan = parse_plan_from_tool_calls(tool_calls)
    if plan is None or not plan.steps:
        return None
    return plan


async def adjust_plan(
    original_request: str,
    current_plan: Plan,
    feedback: str,
    available_tools: List[ToolDict]
) -> Plan:
    """根据用户反馈调整计划，通过 function calling 提交调整后的计划"""
    plan_json = json.dumps(
        {"steps": [s.model_dump(exclude_none=True) for s in current_plan.steps]},
        ensure_ascii=False, indent=2
    )
    plan_tools = _build_plan_tools(available_tools)

    user_prompt = f"""原任务：{original_request}

当前计划：
{plan_json}

用户反馈：{feedback}"""

    try:
        content, tool_calls, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": PLAN_ADJUSTMENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ], stream=False, tools=plan_tools),
            timeout=PLAN_GENERATION_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.error(f"计划调整超时({PLAN_GENERATION_TIMEOUT}秒)")
        return current_plan
    except Exception as e:
        logger.error(f"API调用失败: {e}")
        return current_plan

    try:
        plan = parse_plan_from_tool_calls(tool_calls or {})
        if plan is None or not plan.steps:
            logger.warning("调整后未获得有效计划，保留原计划")
            return current_plan
        return plan
    except Exception as e:
        logger.error(f"解析调整后计划失败: {e}, 原计划保留")
        return current_plan
