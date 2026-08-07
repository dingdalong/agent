"""AgentApp — 外层 REPL 和会话生命周期管理。"""

import asyncio
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field

from rich.text import Text

from src.agent import Agent, AgentDeps
from src.agent.states import RunResult
from src.commands import CommandContext
from src.interfaces.output_router import OutputRouter
from src.interfaces.agent_view_store import AgentViewStore
from src.app.plan_mode_controller import PlanModeController
from src.events.types import InterruptRequested
from src.mgr.data_guard import register_runtime_secrets
from src.mgr.features import resolve_features
from src.mgr.session_mgr import ResumeResult
from src.mgr.session_state import SessionState

logger = logging.getLogger(__name__)


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass
class AgentApp:
    """应用主循环，管理 REPL 交互和 Agent 执行。"""

    deps: AgentDeps = field(repr=False)
    agent_view_store: AgentViewStore = field(repr=False)
    output_router: OutputRouter = field(repr=False)
    _work_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _plan_mode_controller: PlanModeController | None = field(default=None, init=False, repr=False)

    async def run(self) -> None:
        """启动主 REPL 循环。

        Returns:
            None.
        """
        consumer_task = None
        try:
            await self.deps.ui.start()
            consumer_task = asyncio.create_task(self._consume_events())
            await asyncio.sleep(0)

            agent = await self.reset_session(source="startup")
            await self.deps.event_bus.request_output(self._startup_banner())
            while True:
                result = await self._run_agent_turn(agent)
                if result is None:
                    continue
                if result.exit_requested:
                    break
                if result.command is not None:
                    name, args = result.command
                    outcome = await self.deps.command_mgr.dispatch(
                        name, args,
                        CommandContext(deps=self.deps, agent=agent, app=self),
                    )
                    if outcome.new_agent is not None:
                        agent = outcome.new_agent
                    continue
        finally:
            if self.deps.hooks_mgr is not None:
                try:
                    await self.deps.hooks_mgr.run_event(
                        "SessionEnd",
                        "exit",
                        {"reason": "exit"},
                        session_id=self.deps.session_id,
                    )
                except Exception:
                    pass
            if self.deps.event_bus:
                self.deps.event_bus.close()
            if consumer_task:
                consumer_task.cancel()
                await asyncio.gather(consumer_task, return_exceptions=True)
            await self.deps.ui.stop()

    async def _consume_events(self) -> None:
        """消费事件总线上的事件并通过 OutputRouter 分发。

        InterruptRequested 内联处理；其余经唯一的 output_router.dispatch 分流。

        Returns:
            None.
        """
        async for event in self.deps.event_bus.subscribe():
            if isinstance(event, InterruptRequested):
                self._handle_interrupt_requested()
                continue
            await self.output_router.dispatch(event)

    async def _run_agent_turn(self, agent: Agent) -> RunResult | None:
        """执行 agent 对话循环。

        Agent 内部循环多轮对话（REQUEST_INPUT → LLM → DONE → REQUEST_INPUT），
        仅在 exit 或 /clear 时返回 RunResult。本方法负责任务调度和中断处理。

        Args:
            agent: Agent 实例。

        Returns:
            RunResult 表示正常完成；None 表示被中断。
        """
        self._work_task = asyncio.create_task(agent.run())
        with self.deps.ui.watch_interrupt(self._request_interrupt):
            try:
                result = await self._work_task
                if _current_task_is_cancelling():
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                if _current_task_is_cancelling():
                    await self._cancel_and_wait_for_current_work()
                    raise
                await self._handle_interrupted_turn()
                return None
            finally:
                self._work_task = None

    def _request_interrupt(self) -> None:
        """把 UI 中断请求发布到事件总线。

        Returns:
            None.
        """
        asyncio.create_task(self.deps.event_bus.request_interrupt(source="ui"))

    def _install_plan_mode_controller(self) -> None:
        """为当前会话安装入口主 agent 的 Plan 协调器。

        Returns:
            None.
        """
        ui = getattr(self.deps, "ui", None)
        event_bus = getattr(self.deps, "event_bus", None)
        if ui is None or event_bus is None:
            return
        self._plan_mode_controller = PlanModeController(ui=ui, event_bus=event_bus)

    def _refresh_slash_commands(self) -> None:
        """用 CommandMgr 的最新命令集刷新 UI 补全数据源（feature 门控对齐当前角色）。"""
        command_mgr = getattr(self.deps, "command_mgr", None)
        ui = getattr(self.deps, "ui", None)
        if command_mgr is None or ui is None or not hasattr(ui, "set_slash_commands"):
            return
        role_mgr = getattr(self.deps, "role_mgr", None)
        feats = resolve_features(
            role_mgr.manifest.features if role_mgr is not None and role_mgr.manifest else None
        )
        ui.set_slash_commands(command_mgr.completion_items(feats))

    async def reset_session(
        self,
        *,
        source: str = "clear",
    ) -> Agent:
        """Reset shared session state and construct a new foreground Agent.

        The EventBus and UI rejection gates drain old consumer work before shared
        state changes, reject reset-time requests, and drain reset-time events
        before reopening. Frontend runners also finish before Managers, Store,
        context, and Agent state are replaced.

        Args:
            source: Reset source, either ``startup`` or ``clear``.

        Returns:
            Newly constructed foreground Agent.
        """
        trust_gate = getattr(self.deps, "trust_gate", None)
        pending_trust: bool | None = None
        if source == "clear" and trust_gate is not None:
            async def confirm_project_trust(prompt: str) -> bool:
                event_bus = getattr(self.deps, "event_bus", None)
                if event_bus is None:
                    return False
                choice = await event_bus.request_choice(
                    prompt,
                    [
                        ("restricted", "以受限模式继续"),
                        ("trust", "信任并加载"),
                    ],
                    0,
                    source="project_trust",
                )
                return choice == "trust"

            pending_trust = await trust_gate.ensure_trusted(confirm_project_trust)

        event_bus = getattr(self.deps, "event_bus", None)
        bus_gate = event_bus.reject_ui_requests() if event_bus is not None else nullcontext()
        async with self.deps.ui.reset_session_interactions():
            with bus_gate:
                try:
                    if event_bus is not None:
                        await event_bus.join()
                    old_session_id = getattr(self.deps, "session_id", "")
                    old_state = getattr(self.deps, "session_state", None)
                    session_mgr = getattr(self.deps, "session_mgr", None)
                    if old_session_id and old_state is not None and session_mgr is not None:
                        session_mgr.save_state(old_session_id, old_state)
                    self.deps.session_id = str(uuid.uuid4())
                    self.deps.session_state = SessionState()
                    bind_state = getattr(self.output_router, "bind_session_state", None)
                    if bind_state is not None:
                        bind_state(self.deps.session_state)
                    data_guard = getattr(self.deps, "data_guard", None)
                    config_mgr = getattr(self.deps, "config_mgr", None)
                    global_dir = getattr(self.deps, "global_dir", None)
                    workdir = getattr(self.deps, "workdir", None)
                    mcp_mgr = getattr(self.deps, "mcp_mgr", None)
                    tools_mgr = getattr(self.deps, "tools_mgr", None)

                    if source == "clear":
                        if mcp_mgr is not None:
                            await mcp_mgr.stop()
                        if tools_mgr is not None:
                            tools_mgr.unregister_origin("mcp")

                        if pending_trust is not None and config_mgr is not None:
                            config_mgr.set_project_trusted(pending_trust)
                        trusted = bool(getattr(config_mgr, "project_trusted", False))
                        if data_guard is not None and config_mgr is not None and global_dir is not None and workdir is not None:
                            register_runtime_secrets(
                                data_guard, config_mgr, global_dir, workdir, trusted
                            )

                        role_mgr = getattr(self.deps, "role_mgr", None)
                        if role_mgr is not None and hasattr(role_mgr, "reload"):
                            role_mgr.reload()
                        plugin_mgr = getattr(self.deps, "plugin_mgr", None)
                        if plugin_mgr is not None:
                            plugin_mgr.project_trusted = trusted
                            plugin_mgr.reload()
                        hooks_mgr = getattr(self.deps, "hooks_mgr", None)
                        if hooks_mgr is not None:
                            hooks_mgr.project_trusted = trusted
                            hooks_mgr.base_environment = config_mgr.environment
                            hooks_mgr.reload()
                        llm_mgr = getattr(self.deps, "llm_mgr", None)
                        if llm_mgr is not None and hasattr(llm_mgr, "reconfigure"):
                            await llm_mgr.reconfigure()
                        if mcp_mgr is not None:
                            mcp_mgr.project_trusted = trusted
                            await mcp_mgr.start()

                        # 命令注册表随信任刷新重扫（项目层门控依赖 trusted），并刷新补全
                        command_mgr = getattr(self.deps, "command_mgr", None)
                        if command_mgr is not None:
                            command_mgr.project_trusted = trusted
                            command_mgr.reload()
                            self._refresh_slash_commands()
                    elif data_guard is not None and config_mgr is not None and global_dir is not None and workdir is not None:
                        register_runtime_secrets(
                            data_guard, config_mgr, global_dir, workdir,
                            bool(getattr(config_mgr, "project_trusted", False)),
                        )

                    for attr in ("memory_mgr", "tools_mgr", "plan_mgr", "ui"):
                        mgr = getattr(self.deps, attr, None)
                        if mgr is not None and hasattr(mgr, "reload"):
                            mgr.reload()
                    self.agent_view_store.reset()
                    self.deps.session_context.clear()
                    self._install_plan_mode_controller()
                    self.deps.plan_mode_controller = self._plan_mode_controller
                    agent = Agent.from_manifest(
                        manifest=self.deps.role_mgr.manifest if self.deps.role_mgr else None,
                        deps=self.deps,
                        is_subagent=False,
                    )
                    self.agent_view_store.register_foreground(str(agent.uuid), agent.agent_type)
                    set_history_provider = getattr(self.deps.ui, "set_input_history_provider", None)
                    if set_history_provider is not None:
                        set_history_provider(agent.get_input_history)
                    set_model_info = getattr(self.deps.ui, "set_model_info_provider", None)
                    if set_model_info is not None:
                        set_model_info(
                            lambda a=agent: (
                                a.llm.model,
                                a.reasoning_effort or a.llm.reasoning_effort,
                            )
                        )
                    if self._plan_mode_controller is not None:
                        self._plan_mode_controller.install_shortcut(agent)
                        self._plan_mode_controller.notify_state_changed()
                    replace_state = getattr(self.deps.ui, "replace_session_state", None)
                    if replace_state is not None:
                        await replace_state(self.deps.session_state)
                    await self._run_session_start_hooks(source=source)
                    return agent
                finally:
                    if event_bus is not None:
                        await event_bus.join()

    async def resume_session(self, result: ResumeResult) -> tuple[Agent, str]:
        """在 UI/EventBus 门控内保存源会话并切换到目标 SessionState。"""
        event_bus = self.deps.event_bus
        bus_gate = event_bus.reject_ui_requests() if event_bus is not None else nullcontext()
        async with self.deps.ui.reset_session_interactions():
            with bus_gate:
                if event_bus is not None:
                    await event_bus.join()
                session_mgr = self.deps.session_mgr
                source_id = self.deps.session_id
                source_state = self.deps.session_state
                if session_mgr is not None and source_id and source_state is not None:
                    session_mgr.save_state(source_id, source_state)

                self.deps.session_id = result.session_id
                self.deps.session_state = result.state
                self.output_router.bind_session_state(result.state)
                self.agent_view_store.reset()
                self.deps.session_context.clear()
                self._install_plan_mode_controller()
                self.deps.plan_mode_controller = self._plan_mode_controller

                target_plan_active = result.metadata.get("plan_active") is True
                agent = Agent.from_manifest(
                    manifest=self.deps.role_mgr.manifest if self.deps.role_mgr else None,
                    deps=self.deps,
                    is_subagent=False,
                    plan_active=target_plan_active,
                )
                if agent.plan_active != target_plan_active:
                    agent.set_plan_active(target_plan_active)
                self.agent_view_store.register_foreground(str(agent.uuid), agent.agent_type)
                restore_subagents = getattr(self.agent_view_store, "restore_subagents", None)
                if restore_subagents is not None:
                    restore_subagents(result.state.subagent_views())
                set_history_provider = getattr(self.deps.ui, "set_input_history_provider", None)
                if set_history_provider is not None:
                    set_history_provider(agent.get_input_history)
                set_model_info = getattr(self.deps.ui, "set_model_info_provider", None)
                if set_model_info is not None:
                    set_model_info(
                        lambda a=agent: (
                            a.llm.model,
                            a.reasoning_effort or a.llm.reasoning_effort,
                        )
                    )
                if self._plan_mode_controller is not None:
                    self._plan_mode_controller.install_shortcut(agent)
                    self._plan_mode_controller.notify_state_changed()
                await self.deps.ui.replace_session_state(result.state)

                topic = result.metadata.get("topic", "")
                self.deps.session_context.append(
                    f"当前会话已从历史会话恢复（session {result.session_id[:8]}...）。"
                    f"会话主题: \"{topic}\"。请基于恢复的上下文继续对话。"
                )
                agent._prompt_mgr.invalidate_cache()
                task_info = ""
                if agent._task_mgr is not None and agent._task_mgr.has_open_items():
                    tasks = agent._task_mgr.list_tasks()["tasks"]
                    open_count = sum(1 for task in tasks if task["status"] != "completed")
                    task_info = f"，{open_count} 个未完成任务"
                plan_info = "，Plan: active" if agent.plan_active else ""
                summary = (
                    f"已恢复会话 {result.session_id[:8]}..."
                    f"（{len(result.state.context_ids)} 条消息{task_info}{plan_info}）\n"
                )
                if event_bus is not None:
                    await event_bus.join()
                return agent, summary

    def _handle_interrupt_requested(self) -> None:
        """处理中断请求：取消工作任务和活跃输入。"""
        self._cancel_current_work()
        self.deps.ui.cancel_active_input()

    def _cancel_current_work(self) -> bool:
        """取消当前正在执行的 agent 任务。"""
        if self._work_task is None or self._work_task.done():
            return False
        self._work_task.cancel()
        return True

    async def _cancel_and_wait_for_current_work(self) -> None:
        """取消并等待当前 agent 任务完成。"""
        task = self._work_task
        self._cancel_current_work()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _handle_interrupted_turn(self) -> None:
        """收束当前工作与输入任务并输出中断提示。

        Returns:
            None.
        """
        await self._cancel_and_wait_for_current_work()
        self.deps.ui.cancel_active_input()
        await self.deps.event_bus.request_output("\n已中断当前任务。\n")
        await self.deps.event_bus.join()
        session_mgr = getattr(self.deps, "session_mgr", None)
        session_state = getattr(self.deps, "session_state", None)
        session_id = getattr(self.deps, "session_id", "")
        if session_mgr is not None and session_state is not None and session_id:
            session_mgr.save_state(session_id, session_state)
        await self.deps.ui.wait_interactions_idle()

    async def shutdown(self) -> None:
        """断开 MCP server 连接。

        Returns:
            None.
        """
        if self.deps.mcp_mgr is not None:
            await self.deps.mcp_mgr.stop()

    async def _run_session_start_hooks(self, source: str = "startup") -> None:
        """运行 SessionStart hook 并追加其会话上下文。

        Args:
            source: 会话启动来源。

        Returns:
            None.
        """
        if self.deps.hooks_mgr is None:
            return
        result = await self.deps.hooks_mgr.run_event(
            "SessionStart", source, {"source": source},
            session_id=self.deps.session_id,
        )
        self.deps.session_context.extend(result.additional_context)

    def _startup_banner(self) -> Text:
        """生成包含当前会话信息的中文 Rich 启动卡。

        Returns:
            不含 ANSI 控制码的 Rich 文本；由 UI 按运行环境渲染或降级。
        """
        role_mgr = getattr(self.deps, "role_mgr", None)
        role_name = getattr(role_mgr, "role_name", None)
        manifest = getattr(role_mgr, "manifest", None)
        model = "unknown"
        reasoning_effort = "unknown"
        llm_mgr = getattr(self.deps, "llm_mgr", None)
        if llm_mgr is not None:
            try:
                llm = llm_mgr.get(getattr(manifest, "model", None))
                model = getattr(llm, "model", "unknown")
                reasoning_effort = (
                    getattr(manifest, "reasoning_effort", None)
                    or getattr(llm, "reasoning_effort", "unknown")
                )
            except Exception:
                logger.debug("读取启动横幅的模型信息失败", exc_info=True)
        if not role_name or manifest is None:
            role = "unavailable"
            description = ""
        else:
            description = " ".join(str(getattr(manifest, "description", "") or "").split())
            role = role_name
        plan_state = "active" if getattr(manifest, "start_in_plan_mode", False) else "inactive"

        banner = Text()
        banner.append("╭─ ◆ 智能体工作台", style="bold cyan")
        banner.append("  已就绪", style="bold green")
        banner.append("\n│  模型  ", style="bold cyan")
        banner.append(str(model), style="bold")
        banner.append(" ", style="bold")
        banner.append(str(reasoning_effort), style="bold")
        banner.append("\n│  角色  ", style="bold cyan")
        banner.append(str(role), style="bold")
        if description:
            banner.append(f"  {description}", style="dim")
        banner.append("\n│  Plan  ", style="bold cyan")
        banner.append(plan_state, style="bold yellow")
        banner.append("\n│  工作目录  ", style="bold cyan")
        banner.append(str(self.deps.workdir), style="dim")
        banner.append("\n╰─ ", style="bold cyan")
        banner.append("Enter 提交 · Ctrl+J 换行 · Ctrl+C 中断\n", style="dim")
        return banner
