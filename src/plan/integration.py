import logging
from typing import List, Dict, Any, Optional, Callable, Awaitable

from src.tools.tool_executor import ToolExecutor
from src.plan.models import Plan
from src.plan.planner import generate_plan, adjust_plan, ToolDict
from src.plan.executor import execute_plan
from src.plan.exceptions import PlanError
from config import PLAN_MAX_ADJUSTMENTS

logger = logging.getLogger(__name__)

# 用户确认选项
USER_PROMPT_CONFIRM_OPTIONS = ["确认", "执行", "好的", "ok", "yes"]
USER_PROMPT_FINAL_CONFIRM_YES = "y"

# 默认输出函数，使用 print
def _default_output(msg: str) -> None:
    print(msg)


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
    async_input_func: Callable[[str], Awaitable[str]],
    max_adjustments: int = PLAN_MAX_ADJUSTMENTS,
    output_func: Callable[[str], None] = _default_output
) -> str:
    """
    处理需要规划的用户请求，包含确认和调整循环。
    返回最终执行结果（字符串）。

    Args:
        output_func: 输出回调函数，默认使用 print
    """
    current_plan = None
    original_request = user_input

    for cycle in range(max_adjustments):
        if current_plan is None:
            # 生成初始计划
            try:
                current_plan = await generate_plan(original_request, available_tools)
            except PlanError as e:
                logger.error(f"计划生成失败: {e}")
                return "无法生成有效计划，请简化请求。"
            if not current_plan.steps:
                return "无法生成有效计划，请简化请求。"

        # 展示计划
        output_func("\n📋 我为你制定了以下计划：")
        for i, step in enumerate(current_plan.steps, 1):
            output_func(f"{i}. {step.description}")

        # 询问用户
        output_func("\n是否执行此计划？输入 '确认' 开始执行，或输入修改意见（如 '修改步骤2为...'）。")
        user_feedback = await async_input_func("你的反馈：")

        if user_feedback.lower() in USER_PROMPT_CONFIRM_OPTIONS:
            # 确认执行
            result_dict = await execute_plan(current_plan, tool_executor, async_input_func)
            logger.debug(f"执行结果: {result_dict}")
            return format_execution_results(current_plan, result_dict)
        else:
            # 调整计划
            current_plan = await adjust_plan(original_request, current_plan, user_feedback, available_tools)
            output_func("\n已根据你的意见调整计划。")

    # 达到最大调整次数，询问是否执行当前计划
    output_func("\n已达到最大调整次数，是否仍要执行当前计划？(y/n)")
    final_confirm = await async_input_func()
    if final_confirm.lower() == USER_PROMPT_FINAL_CONFIRM_YES:
        result_dict = await execute_plan(current_plan, tool_executor, async_input_func)
        return format_execution_results(current_plan, result_dict)
    else:
        return "计划已取消。"
