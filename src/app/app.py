"""AgentApp — 消息路由和 REPL。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.events.bus import EventBus
from src.guardrails import Guardrail, run_guardrails
from src.agents import RunContext, DynamicState, AppState, AgentDeps
from src.graph import GraphEngine, CompiledGraph
from src.skills.manager import SkillManager
from src.mcp.manager import MCPManager
from src.plan.flow import PlanFlow

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
        input_guardrails: list[Guardrail],
        graph: CompiledGraph,
        skill_manager: SkillManager,
        mcp_manager: MCPManager,
        conversation_buffer: ConversationBuffer | None = None,
        event_bus: EventBus | None = None,
    ):
        self.deps = deps
        self.input_guardrails = input_guardrails
        self.graph = graph
        self.skill_manager = skill_manager
        self.mcp_manager = mcp_manager
        self.conversation_buffer = conversation_buffer
        self.event_bus = event_bus

    async def process(self, user_input: str) -> None:
        """处理单条用户消息。"""
        block = await run_guardrails(self.input_guardrails, None, user_input)
        if block:
            await self.deps.ui.display(f"\n[安全拦截] {block.message}\n")
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
            await self.deps.ui.display("\n请在 /plan 后输入你的请求\n")
            return
        plan_flow = PlanFlow(
            llm=self.deps.llm,
            tool_router=self.deps.tool_router,
            agent_registry=self.deps.agent_registry,
            engine=self.deps.graph_engine,
            ui=self.deps.ui,
        )
        result = await plan_flow.run(plan_request)
        await self.deps.ui.display(f"\n{result}\n")

    async def _handle_skill(self, user_input: str, skill_name: str) -> None:
        """通过 SkillWorkflowParser + WorkflowCompiler 执行 skill 工作流。"""
        from src.skills.workflow_parser import SkillWorkflowParser
        from src.skills.compiler import WorkflowCompiler
        from src.agents.agent import Agent

        # 1. 激活 skill
        content = self.skill_manager.activate(skill_name)
        if not content:
            return
        remaining = user_input[len(f"/{skill_name}"):].strip()
        actual_input = remaining or f"已激活 {skill_name} skill，请按指令执行。"

        # 2. 解析 → WorkflowPlan（携带 full_body）
        parser = SkillWorkflowParser()
        workflow = parser.parse(content, skill_name)

        # 3. 构建共享 system prompt（所有步骤相同，prompt cache 友好）
        constraint_text = ""
        if workflow.constraints:
            lines = "\n".join(f"- {c}" for c in workflow.constraints)
            constraint_text = f"\n\n## 约束\n{lines}"

        shared_system_prompt = (
            f"## 技能文档\n{workflow.full_body}"
            f"\n\n## 用户需求\n{actual_input}"
            f"{constraint_text}"
        )

        # 4. agent_factory：共享 instructions，步骤信息在 task 中
        def make_step_agent(step_id: str, step_name: str, checklist_desc: str) -> Agent:
            return Agent(
                name=f"step_{step_id}",
                description=f"Workflow step: {step_id}",
                instructions=shared_system_prompt,
                task=f"请执行步骤「{step_name}」：{checklist_desc}",
                handoffs=[],
            )

        # 5. 编译 → CompiledGraph
        compiler = WorkflowCompiler()
        skill_graph = compiler.compile(
            workflow,
            agent_factory=make_step_agent,
            skill_manager=self.skill_manager,
        )

        # 6. 构建隔离的执行上下文
        skill_engine = GraphEngine()
        ctx = RunContext(
            input=actual_input,
            state=DynamicState(),
            deps=self.deps,
        )

        # 7. 执行
        result = await skill_engine.run(skill_graph, ctx)

        # 8. 输出
        output = result.output
        if isinstance(output, dict):
            text = output.get("text", str(output))
        elif hasattr(output, "text"):
            text = output.text
        else:
            text = str(output)

        await self.deps.ui.display(f"\n{text}\n")

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
        result = await self.deps.graph_engine.run(self.graph, ctx)
        output = result.output
        if isinstance(output, dict):
            output = output.get("text", str(output))
        elif hasattr(output, "text"):
            output = output.text
        else:
            output = str(output)
        # 流式模式下 TokenDelta 已逐字输出，只补换行；无 EventBus 时才整体打印
        if self.event_bus:
            await self.deps.ui.display("\n")
        else:
            await self.deps.ui.display(f"\n{output}\n")

        # --- Post-turn: 记忆存储 ---
        if self.conversation_buffer is not None:
            self.conversation_buffer.add_assistant_message(output)

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
        # 启动事件消费
        consumer_task = None
        if self.event_bus:
            async def _consume():
                async for event in self.event_bus.subscribe():
                    await self.deps.ui.on_event(event)
            consumer_task = asyncio.create_task(_consume())

        await self.deps.ui.display("Agent 已启动，输入 'exit' 退出。\n")
        try:
            while True:
                user_input = await self.deps.ui.prompt("\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break
                await self.process(user_input)
        finally:
            if self.event_bus:
                self.event_bus.close()
            if consumer_task:
                await consumer_task

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
