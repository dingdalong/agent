"""PlanningFlow：计划生成与执行流程，替代 src/plan/integration.py。

状态流转：
  clarify → generating → confirming ⇄ adjusting → executing → [executing_sensitive] → done
  任意可取消状态 → cancelled
"""

import logging
from typing import Any, Dict, List, Optional

from statemachine import StateMachine, State

from src.core.fsm import FlowModel, OUTPUT_PREFIX
from src.plan.planner import (
    generate_plan,
    adjust_plan,
    classify_user_feedback,
    check_clarification_needed,
)
from src.plan.executor import execute_plan, DeferredStep, DEFERRED_PLACEHOLDER
from src.plan.models import Plan
from src.plan.exceptions import PlanError
from src.tools import ToolDict
from src.tools.router import ToolRouter
from config import PLAN_MAX_ADJUSTMENTS, PLAN_MAX_CLARIFICATION_ROUNDS

logger = logging.getLogger(__name__)


def format_plan_for_display(plan: Plan) -> str:
    """格式化计划为简洁的展示文本"""
    lines = []
    for i, step in enumerate(plan.steps, 1):
        lines.append(f"  {i}. {step.description}")
    return "\n".join(lines)


def format_execution_results(plan: Plan, result_dict: Dict[str, Any]) -> str:
    """格式化执行结果"""
    output_lines = []
    for step in plan.steps:
        res = result_dict.get(step.id, "无结果")
        if res == DEFERRED_PLACEHOLDER:
            continue
        output_lines.append(f"{step.description}: {res}")
    return "\n".join(output_lines)


def _format_tool_args(args: Dict[str, Any]) -> str:
    if not args:
        return ""
    lines = []
    for key, value in args.items():
        val_str = str(value)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        lines.append(f"    {key}: {val_str}")
    return "\n".join(lines)


class PlanningModel(FlowModel):
    """PlanningFlow 专用 model。"""

    def __init__(self, available_tools: List[ToolDict], tool_executor: ToolRouter):
        super().__init__()
        self.available_tools = available_tools
        self.tool_executor = tool_executor
        self.gathered_info_parts: List[str] = []
        self.current_plan: Optional[Plan] = None
        self.adjustment_count: int = 0
        self.clarification_round: int = 0
        self.result_dict: Dict[str, Any] = {}
        self.deferred_steps: List[DeferredStep] = []


class PlanningFlow(StateMachine):
    """计划流程：信息收集 → 生成 → 确认/调整 → 执行"""

    # === 状态 ===
    clarify = State(initial=True)
    generating = State()
    confirming = State()
    adjusting = State()
    executing = State()
    executing_sensitive = State()
    done = State(final=True)
    cancelled = State(final=True)

    # === 转移 ===
    proceed = (
        clarify.to(generating, cond="info_sufficient")
        | clarify.to(clarify, cond="need_more_info")
        | generating.to(confirming, cond="plan_generated")
        | generating.to(done, cond="no_plan_needed")
        | confirming.to(executing, cond="user_confirmed")
        | confirming.to(adjusting, cond="user_wants_adjust")
        | confirming.to(executing, cond="max_adjustments_reached")
        | adjusting.to(confirming)
        | executing.to(executing_sensitive, cond="has_deferred")
        | executing.to(done, cond="no_deferred")
        | executing_sensitive.to(done)
    )

    cancel = (
        clarify.to(cancelled)
        | confirming.to(cancelled)
        | adjusting.to(cancelled)
    )

    def __init__(self, available_tools: List[ToolDict], tool_executor: ToolRouter):
        model = PlanningModel(available_tools, tool_executor)
        super().__init__(model=model)

    # === 条件方法 ===

    def info_sufficient(self) -> bool:
        return self.model.data.get("info_sufficient", False)

    def need_more_info(self) -> bool:
        return not self.model.data.get("info_sufficient", False)

    def plan_generated(self) -> bool:
        return self.model.current_plan is not None

    def no_plan_needed(self) -> bool:
        return self.model.current_plan is None

    def user_confirmed(self) -> bool:
        return self.model.data.get("feedback_action") == "confirm"

    def user_wants_adjust(self) -> bool:
        action = self.model.data.get("feedback_action")
        return action == "adjust" and self.model.adjustment_count < PLAN_MAX_ADJUSTMENTS

    def max_adjustments_reached(self) -> bool:
        action = self.model.data.get("feedback_action")
        return action == "adjust" and self.model.adjustment_count >= PLAN_MAX_ADJUSTMENTS

    def has_deferred(self) -> bool:
        return len(self.model.deferred_steps) > 0

    def no_deferred(self) -> bool:
        return len(self.model.deferred_steps) == 0

    # === 状态回调 ===

    async def on_enter_clarify(self):
        """判断信息是否充足，不足则向用户提问。"""
        model = self.model
        gathered_info = "\n".join(model.gathered_info_parts) if model.gathered_info_parts else ""
        original_request = model.data.get("original_request", "")

        if model.clarification_round >= PLAN_MAX_CLARIFICATION_ROUNDS:
            model.data["info_sufficient"] = True
            model.needs_input = False
            return

        question = await check_clarification_needed(original_request, gathered_info)
        if question is None:
            # 信息充足
            model.data["info_sufficient"] = True
            model.needs_input = False
        else:
            # 需要更多信息，输出问题等待用户回答
            model.data["info_sufficient"] = False
            model.data["current_question"] = question
            model.output_text = f"\n{OUTPUT_PREFIX}{question}\n"
            model.needs_input = True
            model.clarification_round += 1

    async def on_exit_clarify(self):
        """用户回答了问题，记录到 gathered_info。"""
        model = self.model
        if model.user_input and model.data.get("current_question"):
            model.gathered_info_parts.append(
                f"问：{model.data['current_question']}\n答：{model.user_input}"
            )

    async def on_enter_generating(self):
        """生成计划。"""
        model = self.model
        original_request = model.data.get("original_request", "")
        context = "\n".join(model.gathered_info_parts)

        try:
            plan = await generate_plan(original_request, model.available_tools, context=context)
        except PlanError as e:
            logger.error(f"计划生成失败: {e}")
            model.output_text = f"\n{OUTPUT_PREFIX}无法生成有效计划，请简化请求。\n"
            model.current_plan = None
            model.result = "无法生成有效计划，请简化请求。"
            model.needs_input = False
            return

        model.current_plan = plan
        model.needs_input = False

    async def on_enter_confirming(self):
        """展示计划，等待用户确认或调整。"""
        model = self.model
        plan = model.current_plan

        plan_display = format_plan_for_display(plan)
        model.output_text = (
            f"\n{OUTPUT_PREFIX}📋 我为你制定了以下计划：\n{plan_display}\n"
            f"{OUTPUT_PREFIX}是否执行此计划？输入 '确认' 开始执行，或输入修改意见。\n"
        )
        model.needs_input = True

    async def prepare_proceed(self):
        """在 send('proceed') 前根据 user_input 设置条件数据。"""
        model = self.model
        if self.confirming in self.configuration and model.user_input and model.current_plan:
            action = await classify_user_feedback(model.user_input, model.current_plan)
            model.data["feedback_action"] = action

    async def on_enter_adjusting(self):
        """根据用户反馈调整计划。"""
        model = self.model
        original_request = model.data.get("original_request", "")
        feedback = model.user_input or ""

        model.current_plan = await adjust_plan(
            original_request, model.current_plan, feedback, model.available_tools
        )
        model.adjustment_count += 1
        model.output_text = f"\n{OUTPUT_PREFIX}已根据你的意见调整计划。\n"
        model.needs_input = False

    async def on_enter_executing(self):
        """执行计划中的非敏感步骤。"""
        model = self.model

        result_dict, deferred = await execute_plan(
            model.current_plan, model.tool_executor, continue_on_error=True
        )
        model.result_dict = result_dict
        model.deferred_steps = deferred

        # 展示已完成步骤
        display = format_execution_results(model.current_plan, result_dict)
        if display:
            model.output_text = f"\n{OUTPUT_PREFIX}{display}\n"

        model.needs_input = False

    async def on_enter_executing_sensitive(self):
        """逐个确认并执行敏感步骤。"""
        model = self.model
        from src.core.io import agent_input, agent_output

        if model.deferred_steps:
            await agent_output(f"\n{OUTPUT_PREFIX}以下操作需要你的确认：\n")
            for ds in model.deferred_steps:
                await agent_output(f"\n  📌 {ds.step.description}\n")
                args_display = _format_tool_args(ds.resolved_args)
                if args_display:
                    await agent_output(f"{args_display}\n")

                tool_name = ds.step.tool_name
                result = await model.tool_executor.route(tool_name, ds.resolved_args)
                model.result_dict[ds.step.id] = result
                await agent_output(f"  ✅ {result}\n")

        model.needs_input = False

    async def on_enter_done(self):
        """设置最终结果。"""
        model = self.model
        if model.current_plan and model.result_dict:
            model.result = format_execution_results(model.current_plan, model.result_dict)
        elif model.result is None:
            model.result = None  # 不需要计划

    async def on_enter_cancelled(self):
        """取消流程。"""
        self.model.output_text = f"\n{OUTPUT_PREFIX}计划已取消。\n"
        self.model.result = "计划已取消。"
