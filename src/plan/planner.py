import asyncio
import json
import logging
from typing import List, Dict, Any, Optional, TypedDict

from src.core.async_api import call_model
from src.plan.models import Plan, Step
from src.plan.exceptions import JSONParseError, APIGenerationError, PlanError
from src.utils.text import extract_json
from config import PLAN_GENERATION_TIMEOUT

logger = logging.getLogger(__name__)

PLAN_GENERATION_SYSTEM_PROMPT = """你是一个任务规划助手。给定用户请求和可用工具，请生成一个逐步执行的任务计划。
请仅输出 JSON，不要包含任何其他说明或 Markdown 代码块标记。
要求：
1. 每个步骤对象包含字段：
- id: 唯一标识（如step1）
- description: 步骤描述
- action: "tool" 或 "subtask" 或 "user_input"
- tool_name: (若action=tool) 工具名
- tool_args: (若action=tool) 参数字典（可包含变量引用，如$step1.result）
- subtask_prompt: (若action=subtask) 子任务的描述
- depends_on: 依赖的步骤id列表（可选）

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
输出格式必须与原计划相同，为JSON对象，包含"steps"数组。
每个步骤对象字段与之前相同。

可用工具：
{tools_desc}

请只输出JSON，不要其他内容。"""


class FunctionInfo(TypedDict):
    name: str
    description: str


class ToolDict(TypedDict):
    function: FunctionInfo


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
        response = extract_json(response)
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

async def generate_plan(user_input: str, available_tools: List[ToolDict], context: str = "") -> Plan:
    """生成初始计划"""
    tools_desc = build_tools_description(available_tools)

    system_prompt = PLAN_GENERATION_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    user_prompt = f"用户请求：{user_input}\n{context}"
    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], stream=False),
            timeout=PLAN_GENERATION_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise APIGenerationError(
            f"计划生成超时({PLAN_GENERATION_TIMEOUT}秒)", api_error=e
        ) from e
    except Exception as e:
        raise APIGenerationError(f"API调用失败: {e}", api_error=e) from e

    return parse_plan_response(response)

async def adjust_plan(
    original_request: str,
    current_plan: Plan,
    feedback: str,
    available_tools: List[ToolDict]
) -> Plan:
    """根据用户反馈调整计划"""
    plan_lines = []
    for i, step in enumerate(current_plan.steps, 1):
        plan_lines.append(f"{i}. {step.description}")
    plan_desc = "\n".join(plan_lines)

    tools_desc = build_tools_description(available_tools)

    system_prompt = PLAN_ADJUSTMENT_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    user_prompt = f"""原任务：{original_request}

当前计划：
{plan_desc}

用户反馈：{feedback}

请根据反馈调整计划。如果用户要求删除某个步骤，则从列表中移除；如果要求添加步骤，请合理插入；如果要求修改步骤，请更新相应步骤。
输出新的计划。"""

    try:
        response, _, _ = await asyncio.wait_for(
            call_model([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], stream=False),
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
