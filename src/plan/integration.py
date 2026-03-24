import asyncio
import logging
from typing import List, Dict, Any, Optional, Callable

from src.tools.tool_executor import ToolExecutor
from src.core.async_api import async_input
from src.plan.models import Plan
from src.plan.planner import generate_plan, adjust_plan, classify_user_feedback, check_clarification_needed
from src.tools import ToolDict
from src.plan.executor import execute_plan
from src.plan.exceptions import PlanError
from config import PLAN_MAX_ADJUSTMENTS, PLAN_MAX_CLARIFICATION_ROUNDS

logger = logging.getLogger(__name__)

USER_PROMPT_FINAL_CONFIRM_YES = "y"
INPUT_PREFIX = "\n你: "
OUTPUT_PREFIX = "助手: "


async def _call_output(on_output: Callable, msg: str) -> None:
    """调用输出回调，支持同步/异步函数"""
    if asyncio.iscoroutinefunction(on_output):
        await on_output(msg)
    else:
        on_output(msg)


async def _call_input(on_input: Callable, prompt: str) -> str:
    """调用输入回调，支持同步/异步函数"""
    if asyncio.iscoroutinefunction(on_input):
        return await on_input(prompt)
    else:
        return await asyncio.to_thread(on_input, prompt)


def format_execution_results(plan: Plan, result_dict: Dict[str, Any]) -> str:
    """格式化执行结果为易读文本

    Args:
        plan: 执行过的计划
        result_dict: 步骤结果字典 (step_id -> result)

    Returns:
        格式化后的字符串
    """
    output_lines = []
    for step in plan.steps:
        res = result_dict.get(step.id, "无结果")
        output_lines.append(f"{step.description}: {res}")
    return "\n".join(output_lines)

async def handle_planning_request(
    user_input: str,
    available_tools: List[ToolDict],
    tool_executor: ToolExecutor,
    max_adjustments: int = PLAN_MAX_ADJUSTMENTS,
    on_output: Optional[Callable] = None,
    on_input: Optional[Callable] = None
) -> Optional[str]:
    """
    处理需要规划的用户请求，包含确认和调整循环。
    返回最终执行结果（字符串）。

    Args:
        on_output: 输出回调，支持同步/异步。默认 print。
        on_input: 输入回调，支持同步/异步。默认 async_input。
    """
    # on_output 是原始输出函数（不自动换行），integration 内部按需加 \n
    if on_output is None:
        on_output = lambda s: print(s, end="", flush=True)
    if on_input is None:
        on_input = async_input

    current_plan = None
    original_request = user_input

    # === 阶段1：信息收集 ===
    gathered_info_parts = []
    for round_idx in range(PLAN_MAX_CLARIFICATION_ROUNDS):
        gathered_info = "\n".join(gathered_info_parts) if gathered_info_parts else ""
        question = await check_clarification_needed(original_request, gathered_info)
        if question is None:
            break  # 信息充足，进入计划生成
        await _call_output(on_output, f"\n{OUTPUT_PREFIX}{question}\n")
        user_answer = await _call_input(on_input, INPUT_PREFIX)
        gathered_info_parts.append(f"问：{question}\n答：{user_answer}")

    # 将收集到的信息拼接为上下文，传给计划生成
    clarification_context = "\n".join(gathered_info_parts)

    # === 阶段2：计划生成与确认 ===
    for cycle in range(max_adjustments):
        if current_plan is None:
            # 生成初始计划
            try:
                current_plan = await generate_plan(
                    original_request, available_tools, context=clarification_context
                )
            except PlanError as e:
                logger.error(f"计划生成失败: {e}")
                return "无法生成有效计划，请简化请求。"
            if current_plan is None:
                return None  # 模型判断不需要计划

        # 展示计划
        await _call_output(on_output, f"\n{OUTPUT_PREFIX}📋 我为你制定了以下计划：\n")
        for i, step in enumerate(current_plan.steps, 1):
            await _call_output(on_output, f"  {i}. {step.description}\n")
        await _call_output(on_output, f"{OUTPUT_PREFIX}是否执行此计划？输入 '确认' 开始执行，或输入修改意见。\n")

        # 询问用户
        user_feedback = await _call_input(on_input, INPUT_PREFIX)

        action = await classify_user_feedback(user_feedback, current_plan)
        if action == "confirm":
            # 确认执行
            result_dict = await execute_plan(current_plan, tool_executor, on_input=on_input)
            logger.debug(f"执行结果: {result_dict}")
            return format_execution_results(current_plan, result_dict)
        else:
            # 调整计划
            current_plan = await adjust_plan(original_request, current_plan, user_feedback, available_tools)
            await _call_output(on_output, f"\n{OUTPUT_PREFIX}已根据你的意见调整计划。\n")

    # 达到最大调整次数，询问是否执行当前计划
    assert current_plan is not None
    await _call_output(on_output, f"\n{OUTPUT_PREFIX}已达到最大调整次数，是否仍要执行当前计划？(y/n)\n")
    final_confirm = await _call_input(on_input, INPUT_PREFIX)
    if final_confirm.lower() == USER_PROMPT_FINAL_CONFIRM_YES:
        result_dict = await execute_plan(current_plan, tool_executor, on_input=on_input)
        return format_execution_results(current_plan, result_dict)
    else:
        return "计划已取消。"
