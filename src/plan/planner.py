import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel
from src.llm.base import LLMProvider
from src.plan.models import Plan, Step
from src.plan.exceptions import JSONParseError, APIGenerationError, PlanError
from src.tools import ToolDict
from src.llm.structured import build_output_schema, parse_output

logger = logging.getLogger(__name__)


# === submit_plan 虚拟工具 ===

_SUBMIT_PLAN_SCHEMA = build_output_schema(
    "submit_plan",
    "提交执行计划。每个步骤用 tool_name 或 agent_name 指定执行方式。如果请求是简单问答或闲聊，不要调用此工具。",
    Plan
)


# === classify_feedback 虚拟工具 ===

class FeedbackClassification(BaseModel):
    action: Literal["confirm", "adjust"]

_CLASSIFY_FEEDBACK_TOOL = build_output_schema(
    "classify_feedback",
    "输出用户反馈的分类结果：confirm=确认执行, adjust=要求调整",
    FeedbackClassification
)


def _build_plan_tools(available_tools: list[ToolDict]) -> list:
    """构建计划生成用的工具列表：真实工具 + submit_plan"""
    return list(available_tools) + [_SUBMIT_PLAN_SCHEMA]


def _build_agent_list_text(available_agents: list[str]) -> str:
    """构建可用 Agent 列表的文本段落"""
    if not available_agents:
        return ""
    agent_list = ", ".join(available_agents)
    return f"\n\n可用的 Agent（可通过 agent_name 委托任务）：{agent_list}"


# === Prompt 模板 ===

CLARIFICATION_SYSTEM_PROMPT = """你是一个任务规划助手。在制定计划之前，你需要判断用户的请求是否提供了足够的信息来生成一个具体可执行的计划。

请根据"用户原始请求"和"已收集的信息"，判断当前掌握的信息是否足够生成一个具体、可执行的计划。

输出规则：
- 如果信息已经充足，只回复一个词：READY
- 如果还缺少关键信息，直接向用户提问（自然语言，不要输出 JSON）

判断标准：
- 对照用户的原始请求，思考要完成这件事需要哪些关键信息（如时间、地点、人数、预算、偏好、约束条件等）
- 检查"已收集的信息"中是否已经覆盖了这些关键要素
- 如果已收集的信息已经涵盖了主要要素，即使不完美也应该返回 READY，不要过度追问
- 只针对尚未回答的关键信息提问，绝对不要重复问已经回答过的问题
- 提问要自然友好，像朋友对话一样，每次聚焦 2-3 个问题

重要：关注执行所需的信息
- 如果计划涉及发邮件、发消息等操作，必须确认收件人/接收方信息
- 如果涉及文件保存，确认文件名或保存位置偏好
- 绝不要凭空编造用户未提供的信息（如邮箱地址、电话号码、姓名等）"""

PLAN_GENERATION_SYSTEM_PROMPT_TEMPLATE = """你是一个任务规划助手。根据用户请求，判断是否需要生成多步骤计划。

规则：
- 如果请求是简单的问答、闲聊或单步操作，直接回复文本，不要调用 submit_plan
- 如果请求需要多个步骤协作完成，调用 submit_plan 工具提交计划

步骤类型（二选一）：
- tool_name: 调用工具执行操作，tool_args 指定参数。tool_name 和 tool_args 必须严格匹配可用工具定义
- agent_name: 委托给指定 Agent 执行。agent_prompt 描述委托任务和上下文

依赖与变量：
- 步骤之间通过 depends_on 声明依赖关系
- 后续步骤可用 $step_id 或 $step_id.field 引用前序步骤的结果

严格禁止：
- 绝不要在 tool_args 中编造用户未提供的信息（如邮箱地址、电话号码等）{agent_list}"""

PLAN_ADJUSTMENT_SYSTEM_PROMPT_TEMPLATE = """你是一个任务规划助手。用户对当前计划提出了修改意见，请调整计划，然后调用 submit_plan 提交。

步骤类型（二选一）：
- tool_name + tool_args: 工具调用
- agent_name + agent_prompt: Agent 委托

规则：
- 如果用户要求删除某个步骤，从列表中移除
- 如果要求添加步骤，合理插入
- 如果要求修改步骤，更新相应步骤
- tool_name 和 tool_args 必须严格匹配可用工具定义{agent_list}"""

CONFIRM_CLASSIFICATION_PROMPT = """判断用户的回复是"确认执行计划"还是"要求调整计划"。

当前计划：
{plan_summary}

用户回复：{user_feedback}

请调用 classify_feedback 工具输出分类结果：
- 如果用户表示同意、确认、执行、没问题等意思，action 为 confirm
- 如果用户提出修改意见、补充要求、质疑等，action 为 adjust"""


# === 解析函数 ===

def parse_plan_from_tool_calls(tool_calls: dict[int, dict[str, str]]) -> Optional[Plan]:
    """从 tool_calls 中解析 submit_plan 调用，返回 Plan 对象。"""
    return parse_output(tool_calls, "submit_plan", Plan)


# === 核心功能 ===

async def classify_user_feedback(user_feedback: str, plan: Plan, *, llm: LLMProvider) -> str:
    """使用 LLM 判断用户反馈是确认还是调整"""
    plan_summary = "\n".join(
        f"{i}. {step.description}" for i, step in enumerate(plan.steps, 1)
    )
    prompt = CONFIRM_CLASSIFICATION_PROMPT.format(
        plan_summary=plan_summary, user_feedback=user_feedback
    )

    try:
        response = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0,
            tools=[_CLASSIFY_FEEDBACK_TOOL],
            silent=True,
        )
        result = parse_output(response.tool_calls, "classify_feedback", FeedbackClassification)
        if result and result.action in ("confirm", "adjust"):
            return result.action
        return "adjust"
    except Exception as e:
        logger.warning(f"分类用户反馈失败: {e}, 默认为调整")
        return "adjust"


async def check_clarification_needed(user_input: str, gathered_info: str = "", *, llm: LLMProvider) -> Optional[str]:
    """判断信息是否充足。充足返回 None，否则返回追问内容。"""
    user_message = f"用户原始请求：{user_input}\n\n已收集的信息：\n{gathered_info or '（暂无）'}"

    try:
        response = await llm.chat([
            {"role": "system", "content": CLARIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ], temperature=0)
        text = response.content
        if text.strip().upper() == "READY":
            return None
        return text
    except Exception as e:
        logger.warning(f"信息充分性判断失败: {e}，跳过")
        return None


async def generate_plan(
    user_input: str,
    available_tools: list[ToolDict],
    available_agents: list[str],
    context: str = "",
    *,
    llm: LLMProvider,
) -> Optional[Plan]:
    """生成计划。LLM 判断不需要时返回 None。"""
    agent_list_text = _build_agent_list_text(available_agents)
    system_prompt = PLAN_GENERATION_SYSTEM_PROMPT_TEMPLATE.format(agent_list=agent_list_text)

    plan_tools = _build_plan_tools(available_tools)
    user_prompt = f"用户请求：{user_input}\n{context}"

    try:
        response = await llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=plan_tools,
            silent=True,
        )
        content, tool_calls = response.content, response.tool_calls
    except Exception as e:
        raise APIGenerationError(f"API调用失败: {e}", api_error=e) from e

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
    available_tools: list[ToolDict],
    available_agents: list[str],
    *,
    llm: LLMProvider,
) -> Plan:
    """根据反馈调整计划。失败时返回原计划。"""
    agent_list_text = _build_agent_list_text(available_agents)
    system_prompt = PLAN_ADJUSTMENT_SYSTEM_PROMPT_TEMPLATE.format(agent_list=agent_list_text)

    plan_json = json.dumps(
        {"steps": [s.model_dump(exclude_none=True) for s in current_plan.steps]},
        ensure_ascii=False, indent=2,
    )
    plan_tools = _build_plan_tools(available_tools)

    user_prompt = f"""原任务：{original_request}

当前计划：
{plan_json}

用户反馈：{feedback}"""

    try:
        response = await llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=plan_tools,
            silent=True,
        )
        content, tool_calls = response.content, response.tool_calls
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
