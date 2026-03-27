"""PlanFlow — 计划编排流程。

完整流程：澄清 → 生成 → 确认/调整 → 编译 → 执行。
所有用户交互通过 UserInterface 协议，不直接依赖 CLI。
"""

from __future__ import annotations

from src.agents.context import RunContext, DictState
from src.agents.deps import AgentDeps
from src.agents.registry import AgentRegistry
from src.graph import GraphEngine
from src.llm.base import LLMProvider
from src.plan.models import Plan
from src.plan.planner import (
    generate_plan,
    adjust_plan,
    classify_user_feedback,
    check_clarification_needed,
)
from src.plan.compiler import PlanCompiler
from src.tools.router import ToolRouter
# Module defaults (overridable via config.yaml plan section)
PLAN_MAX_CLARIFICATION_ROUNDS = 3
PLAN_MAX_ADJUSTMENTS = 3


class PlanFlow:
    """计划编排流程：澄清 → 生成 → 确认 → 编译 → 执行。"""

    def __init__(
        self,
        tool_router: ToolRouter,
        agent_registry: AgentRegistry,
        engine: GraphEngine,
        ui,
        llm: LLMProvider,
    ):
        self.tool_router = tool_router
        self.agent_registry = agent_registry
        self.engine = engine
        self.ui = ui
        self.llm = llm

    async def run(self, user_input: str) -> str:
        """执行完整计划流程，返回结果文本。"""
        available_tools = self.tool_router.get_all_schemas()
        available_agents = [a.name for a in self.agent_registry.all_agents()]

        # 1. 澄清循环
        gathered = ""
        for _ in range(PLAN_MAX_CLARIFICATION_ROUNDS):
            question = await check_clarification_needed(user_input, gathered, llm=self.llm)
            if question is None:
                break
            await self.ui.display(f"\n{question}\n")
            answer = await self.ui.prompt("\n你: ")
            gathered += f"\n{question}\n回答: {answer}"

        # 2. 生成计划
        context = gathered if gathered else ""
        plan = await generate_plan(user_input, available_tools, available_agents, context, llm=self.llm)
        if plan is None:
            return "这个请求不需要多步计划，我直接回答。"

        # 3. 确认/调整循环
        for _ in range(PLAN_MAX_ADJUSTMENTS):
            plan_display = self.format_plan(plan)
            await self.ui.display(f"\n执行计划：\n{plan_display}\n")
            feedback_input = await self.ui.prompt("\n确认执行？(输入 '确认' 或修改意见): ")

            action = await classify_user_feedback(feedback_input, plan, llm=self.llm)
            if action == "confirm":
                break
            plan = await adjust_plan(
                user_input, plan, feedback_input, available_tools, available_agents, llm=self.llm
            )

        # 4. 编译并执行
        compiler = PlanCompiler(self.agent_registry, self.tool_router)
        compiled_graph = compiler.compile(plan)

        ctx = RunContext(
            input=user_input,
            state=DictState(),
            deps=AgentDeps(
                tool_router=self.tool_router,
                agent_registry=self.agent_registry,
                graph_engine=self.engine,
                ui=self.ui,
            ),
        )
        result = await self.engine.run(compiled_graph, ctx)

        # 提取输出
        output = result.output
        if isinstance(output, dict):
            output = output.get("text", str(output))
        return str(output) if output else "计划执行完成。"

    @staticmethod
    def format_plan(plan: Plan) -> str:
        """格式化计划用于展示。"""
        lines = []
        for i, step in enumerate(plan.steps, 1):
            deps = f" (依赖: {', '.join(step.depends_on)})" if step.depends_on else ""
            if step.tool_name:
                lines.append(f"  {i}. [工具] {step.description} -> {step.tool_name}{deps}")
            elif step.agent_name:
                lines.append(f"  {i}. [Agent] {step.description} -> {step.agent_name}{deps}")
        return "\n".join(lines)
