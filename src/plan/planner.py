import asyncio
import json
import logging
from typing import List, Dict, Any, Optional, TypedDict

from src.core.async_api import call_model
from src.plan.models import Plan, Step
from src.plan.exceptions import JSONParseError, APIGenerationError, PlanError
from config import PLAN_GENERATION_TIMEOUT

logger = logging.getLogger(__name__)


class FunctionInfo(TypedDict):
    name: str
    description: str


class ToolDict(TypedDict):
    function: FunctionInfo


def build_step_fields_description() -> str:
    """从 Step model 自动生成字段描述，确保 prompt 与模型定义同步"""
    schema = Step.model_json_schema()
    required = set(schema.get("required", []))
    lines = []
    for name, prop in schema.get("properties", {}).items():
        desc = prop.get("description", "")
        is_required = name in required
        marker = "（必填）" if is_required else "（可选）"
        lines.append(f"- {name}: {desc} {marker}")
    return "\n".join(lines)


_STEP_FIELDS_DESC = build_step_fields_description()

PLAN_GENERATION_SYSTEM_PROMPT = """你是一个任务规划助手。给定用户请求和可用工具，请判断该请求是否适合生成多步骤计划，并在适合时生成计划。

重要规则：
- 如果请求是简单的问答、闲聊、单步操作，或者无法拆解为多个有意义的步骤，请返回 {{"steps": []}}
- 只有当请求确实需要多个步骤协作完成时，才生成计划

请仅输出 JSON，不要包含任何其他说明或 Markdown 代码块标记。
要求：
1. 每个步骤对象包含字段：
""" + _STEP_FIELDS_DESC + """

2. 可用工具：
{tools_desc}

3. 返回严格的 JSON 格式，外层是一个对象，包含一个"steps"数组
实例：
{{
  "steps": [
    {{
      "id": "step1",
      "description": "查询明天天气情况，以决定出行安排",
      "action": "tool",
      "tool_name": "get_weather",
      "tool_args": {{
        "location": "广州"
      }},
      "depends_on": []
    }}
  ]
}}"""

PLAN_ADJUSTMENT_SYSTEM_PROMPT = """你是一个任务规划助手。用户对当前计划提出了修改意见，请根据意见调整计划。

请仅输出 JSON，不要包含任何其他说明或 Markdown 代码块标记。
输出格式为JSON对象，包含"steps"数组。
每个步骤对象包含字段：
""" + _STEP_FIELDS_DESC + """

可用工具：
{tools_desc}"""


def build_tools_description(available_tools: List[ToolDict]) -> str:
    """构建工具描述字符串"""
    return "\n".join([
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in available_tools
    ])


def parse_plan_response(response: str) -> Plan:
    """解析计划响应，返回Plan对象

    Args:
        response: LLM返回的响应文本

    Returns:
        Plan对象

    Raises:
        JSONParseError: JSON解析失败时抛出
    """
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

CONFIRM_CLASSIFICATION_PROMPT = """判断用户的回复是"确认执行计划"还是"要求调整计划"。

当前计划：
{plan_summary}

用户回复：{user_feedback}

请只输出一个JSON对象，格式为：{{"action": "confirm"}} 或 {{"action": "adjust"}}
- 如果用户表示同意、确认、执行、没问题等意思，输出 confirm
- 如果用户提出修改意见、补充要求、质疑等，输出 adjust"""


async def classify_user_feedback(user_feedback: str, plan: Plan) -> str:
    """使用LLM判断用户反馈是确认执行还是要求调整计划

    Args:
        user_feedback: 用户的反馈文本
        plan: 当前计划

    Returns:
        "confirm" 或 "adjust"
    """
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


async def generate_plan(user_input: str, available_tools: List[ToolDict], context: str = "") -> Optional[Plan]:
    """生成初始计划。如果模型判断请求不适合生成计划，返回 None。"""
    tools_desc = build_tools_description(available_tools)

    system_prompt = PLAN_GENERATION_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    user_prompt = f"用户请求：{user_input}\n{context}"
    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], stream=False,
            response_format={"type": "json_object"}),
            timeout=PLAN_GENERATION_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise APIGenerationError(
            f"计划生成超时({PLAN_GENERATION_TIMEOUT}秒)", api_error=e
        ) from e
    except Exception as e:
        raise APIGenerationError(f"API调用失败: {e}", api_error=e) from e

    plan = parse_plan_response(response)
    if not plan.steps:
        return None
    return plan

async def adjust_plan(
    original_request: str,
    current_plan: Plan,
    feedback: str,
    available_tools: List[ToolDict]
) -> Plan:
    """根据用户反馈调整计划"""
    plan_json = json.dumps(
        {"steps": [s.model_dump(exclude_none=True) for s in current_plan.steps]},
        ensure_ascii=False, indent=2
    )

    tools_desc = build_tools_description(available_tools)

    system_prompt = PLAN_ADJUSTMENT_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    user_prompt = f"""原任务：{original_request}

当前计划：
{plan_json}

用户反馈：{feedback}

请根据反馈调整计划。如果用户要求删除某个步骤，则从列表中移除；如果要求添加步骤，请合理插入；如果要求修改步骤，请更新相应步骤。
输出新的计划。"""

    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], stream=False,
            response_format={"type": "json_object"}),
            timeout=PLAN_GENERATION_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.error(f"计划调整超时({PLAN_GENERATION_TIMEOUT}秒)")
        return current_plan
    except Exception as e:
        logger.error(f"API调用失败: {e}")
        return current_plan

    try:
        return parse_plan_response(response)
    except Exception as e:
        logger.error(f"解析调整后计划失败: {e}, 原计划保留")
        return current_plan
