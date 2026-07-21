"""AgentApp — 外层 REPL 和会话生命周期管理。"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from src.agent import Agent, AgentDeps
from src.agent.states import RunResult
from src.interfaces.output_router import OutputRouter
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.status_presenter import present_agent
from src.app.permission_mode_controller import PermissionModeController
from src.events import NoEventSubscribers
from src.events.types import InterruptRequested

logger = logging.getLogger(__name__)


@dataclass
class AgentApp:
    """应用主循环，管理 REPL 交互和 Agent 执行。"""

    deps: AgentDeps = field(repr=False)
    agent_view_store: AgentViewStore = field(repr=False)
    output_router: OutputRouter = field(repr=False)
    _work_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _permission_mode_controller: PermissionModeController | None = field(default=None, init=False, repr=False)

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

            await self.deps.event_bus.request_output(self._startup_banner())
            agent = await self._reset_session(source="startup")
            while True:
                result = await self._run_agent_turn(agent)
                if result is None:
                    continue
                if result.exit_requested:
                    break
                if result.command is not None:
                    cmd_name = result.command[0]
                    if cmd_name == "clear":
                        agent = await self._reset_session(source="clear")
                        await self.deps.event_bus.request_output("上下文已清理，所有组件已重载。\n")
                        continue
                    if cmd_name == "agents":
                        # 复用同一 agent（不 reset）：弹出可选列表并回看子 agent 完整消息记录，会话历史保留
                        await self._browse_subagents()
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

    async def _browse_subagents(self) -> None:
        """处理 /agents 命令：弹出可方向键选择的子 agent 列表，选中后以只读面板回看其完整原始消息记录。

        循环：每轮重取列表（运行中的子 agent 可能已完成）→ request_choice 选择 → 选中则
        request_transcript_view 打开面板 → Esc 返回列表 → 直至列表 Esc 取消退出。非 TTY 环境
        无富交互面板，退回打印纯文本摘要。

        Returns:
            None.
        """
        if not self.deps.ui.is_tty:
            snapshots = self.agent_view_store.subagent_snapshots()
            if not snapshots:
                summary = "本会话尚未启动任何子 agent。"
            else:
                lines = [f"本会话子 agent（{len(snapshots)}）:"]
                lines.extend(present_agent(snapshot).plain for snapshot in snapshots)
                summary = "\n".join(lines)
            await self.deps.event_bus.request_output(summary + "\n")
            return
        while True:
            snapshots = self.agent_view_store.subagent_snapshots()
            choices = [
                (snapshot.uuid, present_agent(snapshot).plain)
                for snapshot in snapshots
            ]
            if not choices:
                await self.deps.event_bus.request_output("暂无子 agent 记录。\n")
                return
            try:
                picked = await self.deps.event_bus.request_choice(
                    "\n子 agent 历史（选择查看完整消息记录）", choices, 0
                )
            except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                return
            if not picked:  # Esc 取消，退出浏览
                return
            try:
                await self.deps.event_bus.request_transcript_view(picked)
            except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                return

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
                return result
            except (asyncio.CancelledError, KeyboardInterrupt):
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

    def _install_permission_mode_controller(self) -> None:
        """为当前会话安装入口主 agent 的权限模式协调器。

        Returns:
            None.
        """
        permission_mgr = getattr(self.deps, "permission_mgr", None)
        ui = getattr(self.deps, "ui", None)
        event_bus = getattr(self.deps, "event_bus", None)
        if permission_mgr is None or ui is None or event_bus is None:
            return
        self._permission_mode_controller = PermissionModeController(
            permission_mgr=permission_mgr,
            ui=ui,
            event_bus=event_bus,
        )

    async def _reset_session(
        self,
        *,
        source: str = "clear",
    ) -> Agent:
        """重置会话状态，创建新的 Agent 实例。

        生成 session_id，使新 Agent 的
        TaskManager 指向空目录（旧 task 文件留在磁盘上可通过 /resume 找回）。

        Args:
            source: 重置来源，"startup" 或 "clear"。

        Returns:
            新创建的 Agent 实例。
        """
        self.deps.session_id = str(uuid.uuid4())
        # UI 清理交互态；AgentViewStore 单独原子清空全部会话状态。
        for attr in ("memory_mgr", "tools_mgr", "permission_mgr",
                     "config_mgr", "plugin_mgr", "hooks_mgr", "plan_mgr",
                     "ui"):
            mgr = getattr(self.deps, attr, None)
            if mgr is not None and hasattr(mgr, "reload"):
                mgr.reload()
        self.agent_view_store.reset()
        self.deps.session_context.clear()
        self._install_permission_mode_controller()
        self.deps.permission_mode_controller = self._permission_mode_controller
        agent = Agent.from_manifest(
            manifest=self.deps.role_mgr.manifest if self.deps.role_mgr else None,
            deps=self.deps,
            is_subagent=False,
        )
        self.agent_view_store.register_foreground(str(agent.uuid), agent.agent_type)
        if self._permission_mode_controller is not None:
            self._permission_mode_controller.install_shortcut(agent)
            self._permission_mode_controller.notify_state_changed()
        await self._run_session_start_hooks(source=source)
        return agent

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

    async def _handle_interrupted_turn(self) -> None:
        """收束当前工作与输入任务并输出中断提示。

        Returns:
            None.
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
        self._cancel_current_work()
        self.deps.ui.cancel_active_input()
        if self._work_task:
            await asyncio.gather(self._work_task, return_exceptions=True)
        await self.deps.event_bus.request_output("\n已中断当前任务。\n")
        await self.deps.event_bus.join()

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

    def _startup_banner(self) -> str:
        """生成包含当前模型、角色、权限模式和工作目录的启动横幅。

        Returns:
            启动时输出的纯文本横幅。
        """
        model = getattr(self.deps.llm_mgr.get(), "model", "unknown") if self.deps.llm_mgr else "unknown"
        role_mgr = getattr(self.deps, "role_mgr", None)
        role_name = getattr(role_mgr, "role_name", None)
        manifest = getattr(role_mgr, "manifest", None)
        if not role_name or manifest is None:
            role = "unavailable"
        else:
            description = getattr(manifest, "description", "").strip()
            role = role_name
            if description:
                role += f" — {description}"
        permission_mode = "unknown"
        if getattr(self.deps, "permission_mgr", None) is not None:
            permission_mode = self.deps.permission_mgr.default_mode.value
        return (
            "Agent workbench ready\n"
            f"model: {model}\n"
            f"role: {role}\n"
            f"permission mode: {permission_mode}\n"
            f"workdir: {self.deps.workdir}\n"
            "Enter submits · Ctrl+J newline · Ctrl+C interrupts · exit/quit to leave · /mode to switch\n"
        )
