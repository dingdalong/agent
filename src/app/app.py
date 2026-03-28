"""AgentApp — 消息路由和 REPL。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.interfaces.base import UserInterface
from src.guardrails import InputGuardrail
from src.agents import RunContext, DynamicState, AppState, AgentDeps, AgentRegistry, AgentRunner
from src.agents.node import AgentNode
from src.graph import GraphEngine, CompiledGraph
from src.tools.router import ToolRouter
from src.skills.manager import SkillManager
from src.mcp.manager import MCPManager
from src.plan.flow import PlanFlow
from src.app.presets import build_skill_graph

if TYPE_CHECKING:
    from src.memory.buffer import ConversationBuffer
    from src.memory.types import MemoryRecord

logger = logging.getLogger(__name__)


class AgentApp:
    """应用核心 — 消息路由 + REPL 循环。

    所有组件由 bootstrap.py 注入，AgentApp 不创建任何具体实现。
    消息路由逻辑：
    - 所有输入先经过 InputGuardrail 安全检查
    - /plan 命令 → PlanFlow 多步骤规划执行
    - /skill-name → SkillManager 激活技能，构建独立图执行
    - 普通消息 → 默认图（orchestrator → 专家智能体）
    """

    def __init__(
        self,
        deps: AgentDeps,
        ui: UserInterface,
        guardrail: InputGuardrail,
        tool_router: ToolRouter,
        agent_registry: AgentRegistry,
        engine: GraphEngine,
        graph: CompiledGraph,
        skill_manager: SkillManager,
        mcp_manager: MCPManager,
        runner: AgentRunner,
        conversation_buffer: ConversationBuffer | None = None,
    ):
        self.deps = deps
        self.ui = ui
        self.guardrail = guardrail
        self.tool_router = tool_router
        self.agent_registry = agent_registry
        self.engine = engine
        self.graph = graph
        self.skill_manager = skill_manager
        self.mcp_manager = mcp_manager
        self.runner = runner
        self.conversation_buffer = conversation_buffer

    async def process(self, user_input: str) -> None:
        """处理单条用户消息。"""
        passed, reason = self.guardrail.check(user_input)
        if not passed:
            await self.ui.display(f"\n[安全拦截] {reason}\n")
            return

        if user_input.strip().startswith("/plan"):
            await self._handle_plan(user_input)
            return

        skill_name = self.skill_manager.is_slash_command(user_input)
        if skill_name:
            await self._handle_skill(user_input, skill_name)
            return

        await self._handle_normal(user_input)

    async def _handle_plan(self, user_input: str) -> None:
        plan_request = user_input.strip()[5:].strip()
        if not plan_request:
            await self.ui.display("\n请在 /plan 后输入你的请求\n")
            return
        plan_flow = PlanFlow(
            llm=self.deps.llm,
            tool_router=self.tool_router,
            agent_registry=self.agent_registry,
            engine=self.engine,
            ui=self.ui,
        )
        result = await plan_flow.run(plan_request)
        await self.ui.display(f"\n{result}\n")

    async def _handle_skill(self, user_input: str, skill_name: str) -> None:
        skill_content = self.skill_manager.activate(skill_name)
        if not skill_content:
            return
        remaining = user_input[len(f"/{skill_name}"):].strip()
        actual_input = remaining or f"已激活 {skill_name} skill，请按指令执行。"
        skill_registry = AgentRegistry()
        skill_runner = AgentRunner(registry=skill_registry)
        skill_graph = build_skill_graph(skill_registry, skill_content, runner=skill_runner)
        skill_engine = GraphEngine()
        ctx = RunContext(
            input=actual_input,
            state=DynamicState(),
            deps=AgentDeps(
                llm=self.deps.llm,
                tool_router=self.tool_router,
                agent_registry=skill_registry,
                graph_engine=skill_engine,
                ui=self.ui,
                memory=self.deps.memory,
            ),
        )
        result = await skill_engine.run(skill_graph, ctx)
        await self.ui.display(f"\n{result.output}\n")

    async def _handle_normal(self, user_input: str) -> None:
        state = AppState()

        # --- Pre-turn: 记忆检索 ---
        if self.conversation_buffer is not None:
            self.conversation_buffer.add_user_message(user_input)

        if self.deps.memory is not None:
            try:
                memories: list[MemoryRecord] = self.deps.memory.search(user_input, n=5)
                if memories:
                    state.memory_context = self._format_memories(memories)
            except Exception:
                logger.warning("[记忆系统] 检索失败，跳过", exc_info=True)

        if self.conversation_buffer is not None:
            state.conversation_history = self.conversation_buffer.get_messages_for_api()

        # --- Execution ---
        ctx = RunContext(
            input=user_input,
            state=state,
            deps=self.deps,
        )
        result = await self.engine.run(self.graph, ctx)
        output = result.output
        if isinstance(output, dict):
            output = output.get("text", str(output))
        await self.ui.display(f"\n{output}\n")

        # --- Post-turn: 记忆存储 ---
        if self.conversation_buffer is not None:
            self.conversation_buffer.add_assistant_message(
                {"role": "assistant", "content": output}
            )

        if self.deps.memory is not None:
            try:
                await self.deps.memory.add_from_conversation(
                    user_input=user_input,
                    assistant_response=output,
                )
            except Exception:
                logger.warning("[记忆系统] 事实提取失败，跳过", exc_info=True)

        if (
            self.conversation_buffer is not None
            and self.deps.memory is not None
            and self.conversation_buffer.should_compress()
        ):
            try:
                await self.conversation_buffer.compress(
                    store=self.deps.memory,
                    llm=self.deps.llm,
                )
            except Exception:
                logger.warning("[记忆系统] 对话压缩失败，跳过", exc_info=True)

    def _format_memories(self, memories: list[MemoryRecord]) -> str:
        """将 MemoryRecord 列表格式化为 LLM 上下文字符串。"""
        lines = []
        for m in memories:
            prefix = "[事实]" if m.memory_type.value == "fact" else "[摘要]"
            lines.append(f"{prefix} {m.content}")
        return "\n".join(lines)

    async def run(self) -> None:
        """CLI 主循环。"""
        await self.ui.display("Agent 已启动，输入 'exit' 退出。\n")
        while True:
            user_input = await self.ui.prompt("\n你: ")
            if user_input.strip().lower() in ("exit", "quit"):
                break
            await self.process(user_input)

    async def shutdown(self) -> None:
        if (
            self.conversation_buffer is not None
            and self.deps.memory is not None
            and len(self.conversation_buffer.messages) > 0
        ):
            try:
                await self.conversation_buffer.compress(
                    store=self.deps.memory,
                    llm=self.deps.llm,
                )
            except Exception:
                logger.warning("[记忆系统] 退出时对话压缩失败", exc_info=True)
        await self.mcp_manager.disconnect_all()
