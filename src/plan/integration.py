import logging
from typing import List, Dict, Any, Optional

from src.tools.tool_executor import ToolExecutor
from src.core.io import agent_input, agent_output
from src.plan.models import Plan
from src.plan.planner import generate_plan, adjust_plan, classify_user_feedback, check_clarification_needed
from src.tools import ToolDict
from src.plan.executor import execute_plan, DeferredStep, DEFERRED_PLACEHOLDER
from src.plan.exceptions import PlanError
from config import PLAN_MAX_ADJUSTMENTS, PLAN_MAX_CLARIFICATION_ROUNDS

logger = logging.getLogger(__name__)

USER_PROMPT_FINAL_CONFIRM_YES = "y"
INPUT_PREFIX = "\n你: "
OUTPUT_PREFIX = "助手: "


def format_plan_for_display(plan: Plan) -> str:
    """格式化计划为简洁的展示文本，只展示步骤描述"""
    lines = []
    for i, step in enumerate(plan.steps, 1):
        lines.append(f"  {i}. {step.description}")
    return "\n".join(lines)


def _log_plan_detail(plan: Plan) -> None:
    """将计划的详细信息记录到日志"""
    for i, step in enumerate(plan.steps, 1):
        detail = f"步骤{i} [{step.action}] {step.description}"
        if step.action == "tool" and step.tool_name:
            detail += f" | 工具: {step.tool_name}({step.tool_args or {}})"
        elif step.action == "subtask" and step.subtask_prompt:
            detail += f" | prompt: {step.subtask_prompt}"
        if step.depends_on:
            detail += f" | 依赖: {step.depends_on}"
        logger.debug(detail)


def format_execution_results(plan: Plan, result_dict: Dict[str, Any]) -> str:
    """格式化执行结果为易读文本，跳过待确认的占位步骤"""
    output_lines = []
    for step in plan.steps:
        res = result_dict.get(step.id, "无结果")
        if res == DEFERRED_PLACEHOLDER:
            continue
        output_lines.append(f"{step.description}: {res}")
    return "\n".join(output_lines)


def _format_tool_args(args: Dict[str, Any]) -> str:
    """将工具参数格式化为用户可读的文本"""
    if not args:
        return ""
    lines = []
    for key, value in args.items():
        val_str = str(value)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        lines.append(f"    {key}: {val_str}")
    return "\n".join(lines)


async def _execute_deferred_steps(
    deferred: List[DeferredStep],
    tool_executor: ToolExecutor,
    result_dict: Dict[str, Any],
) -> None:
    """逐个展示参数内容、确认并执行延迟的敏感工具步骤，结果写入 result_dict"""
    if not deferred:
        return
    await agent_output(f"\n{OUTPUT_PREFIX}以下操作需要你的确认：\n")
    for ds in deferred:
        # 先展示步骤描述和具体参数，让用户看到完整内容
        await agent_output(f"\n  📌 {ds.step.description}\n")
        args_display = _format_tool_args(ds.resolved_args)
        if args_display:
            await agent_output(f"{args_display}\n")
        tool_name = ds.step.tool_name
        assert tool_name is not None  # 由 _is_sensitive_tool_step 保证
        confirmed = await tool_executor._confirm_sensitive(tool_name, ds.resolved_args)
        if confirmed:
            result = await tool_executor.execute(
                tool_name, ds.resolved_args, skip_confirm=True
            )
            result_dict[ds.step.id] = result
            await agent_output(f"  ✅ {result}\n")
        else:
            result_dict[ds.step.id] = "用户取消了操作"
            await agent_output(f"  ❌ 已取消\n")

async def handle_planning_request(
    user_input: str,
    available_tools: List[ToolDict],
    tool_executor: ToolExecutor,
    max_adjustments: int = PLAN_MAX_ADJUSTMENTS,
) -> Optional[str]:
    """
    处理需要规划的用户请求，包含确认和调整循环。
    返回最终执行结果（字符串）。
    """
    current_plan = None
    original_request = user_input

    # === 阶段1：信息收集 ===
    gathered_info_parts = []
    for round_idx in range(PLAN_MAX_CLARIFICATION_ROUNDS):
        gathered_info = "\n".join(gathered_info_parts) if gathered_info_parts else ""
        # check_clarification_needed 流式输出问题给用户，返回问题文本或 None（信息充足）
        question = await check_clarification_needed(original_request, gathered_info)
        if question is None:
            break  # 信息充足，进入计划生成
        await agent_output("\n")
        user_answer = await agent_input(INPUT_PREFIX)
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

        # 展示计划（简洁版给用户，详细版记录日志）
        _log_plan_detail(current_plan)
        plan_display = format_plan_for_display(current_plan)
        await agent_output(f"\n{OUTPUT_PREFIX}📋 我为你制定了以下计划：\n{plan_display}\n")
        await agent_output(f"{OUTPUT_PREFIX}是否执行此计划？输入 '确认' 开始执行，或输入修改意见。\n")

        # 询问用户
        user_feedback = await agent_input(INPUT_PREFIX)

        action = await classify_user_feedback(user_feedback, current_plan)
        if action == "confirm":
            # 阶段 A：执行非敏感步骤
            result_dict, deferred = await execute_plan(
                current_plan, tool_executor, continue_on_error=True
            )
            logger.debug(f"执行结果: {result_dict}, 延迟步骤: {len(deferred)}")

            # 展示已完成步骤的结果
            display = format_execution_results(current_plan, result_dict)
            if display:
                await agent_output(f"\n{OUTPUT_PREFIX}{display}\n")

            # 阶段 B：逐个确认并执行敏感步骤
            await _execute_deferred_steps(deferred, tool_executor, result_dict)

            return format_execution_results(current_plan, result_dict)
        else:
            # 调整计划
            current_plan = await adjust_plan(original_request, current_plan, user_feedback, available_tools)
            await agent_output(f"\n{OUTPUT_PREFIX}已根据你的意见调整计划。\n")

    # 达到最大调整次数，询问是否执行当前计划
    assert current_plan is not None
    await agent_output(f"\n{OUTPUT_PREFIX}已达到最大调整次数，是否仍要执行当前计划？(y/n)\n")
    final_confirm = await agent_input(INPUT_PREFIX)
    if final_confirm.lower() == USER_PROMPT_FINAL_CONFIRM_YES:
        result_dict, deferred = await execute_plan(
            current_plan, tool_executor, continue_on_error=True
        )
        display = format_execution_results(current_plan, result_dict)
        if display:
            await agent_output(f"\n{OUTPUT_PREFIX}{display}\n")
        await _execute_deferred_steps(deferred, tool_executor, result_dict)
        return format_execution_results(current_plan, result_dict)
    else:
        return "计划已取消。"
