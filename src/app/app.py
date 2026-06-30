"""AgentApp — 外层 REPL 和会话生命周期管理。"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from src.agent import Agent, AgentDeps
from src.agent.states import RunResult
from src.interfaces.output_router import OutputRouter
from src.app.permission_mode_controller import PermissionModeController
from src.events.types import InterruptRequested

logger = logging.getLogger(__name__)


@dataclass
class AgentApp:
    """应用主循环，管理 REPL 交互和 Agent 执行。"""

    deps: AgentDeps = field(repr=False)
    output_router: OutputRouter | None = None  # 消费端事件路由器（app 层持有，不入业务依赖容器）
    _work_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _permission_mode_controller: PermissionModeController | None = field(default=None, init=False, repr=False)

    async def run(self) -> None:
        """启动主 REPL 循环。"""
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
                if result.command is not None and result.command[0] == "clear":
                    agent = await self._reset_session(source="clear")
                    await self.deps.event_bus.request_output("上下文已清理，所有组件已重载。\n")
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

        InterruptRequested 内联处理（不变）；其余经 output_router.dispatch 分流。
        output_router 为 None 时回退直接转发 UI（非 bootstrap 构造路径兼容）。
        """
        async for event in self.deps.event_bus.subscribe():
            if isinstance(event, InterruptRequested):
                self._handle_interrupt_requested()
                continue
            if self.output_router is not None:
                await self.output_router.dispatch(event)
            else:
                await self.deps.ui.on_event(event)

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
        asyncio.create_task(self.deps.event_bus.request_interrupt(source="ui"))

    def _install_permission_mode_controller(self) -> None:
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
        # "ui" 一并纳入：ui 清零会话级 token 统计。output_router 由 app 层持有，单独 reload 清空 agent 视图。
        for attr in ("memory_mgr", "tools_mgr", "permission_mgr",
                     "config_mgr", "plugin_mgr", "hooks_mgr", "plan_mgr",
                     "ui"):
            mgr = getattr(self.deps, attr, None)
            if mgr is not None and hasattr(mgr, "reload"):
                mgr.reload()
        if self.output_router is not None:
            self.output_router.reload()
        self.deps.session_context.clear()
        self._install_permission_mode_controller()
        self.deps.permission_mode_controller = self._permission_mode_controller
        agent = Agent(
            agent_type="总控",
            description="入口",
            deps=self.deps,
            role_prompt=self.deps.role_mgr.identity if self.deps.role_mgr else None,
            enable_thinking=self.deps.role_mgr.enable_thinking if self.deps.role_mgr else True,
        )
        if self.output_router is not None:
            self.output_router.set_foreground(str(agent.uuid))
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

    async def shutdown(self):
        """断开 MCP server 连接。与 create_app() 中的 mcp_mgr.start() 同处 main 任务。"""
        if self.deps.mcp_mgr is not None:
            await self.deps.mcp_mgr.stop()

    async def _run_session_start_hooks(self, source: str = "startup") -> None:
        if self.deps.hooks_mgr is None:
            return
        result = await self.deps.hooks_mgr.run_event(
            "SessionStart", source, {"source": source},
            session_id=self.deps.session_id,
        )
        self.deps.session_context.extend(result.additional_context)

    def _startup_banner(self) -> str:
        model = getattr(self.deps.llm_mgr.get(), "model", "unknown") if self.deps.llm_mgr else "unknown"
        permission_mode = "unknown"
        if getattr(self.deps, "permission_mgr", None) is not None:
            permission_mode = self.deps.permission_mgr.default_mode.value
        return (
            "Agent workbench ready\n"
            f"model: {model}\n"
            f"permission mode: {permission_mode}\n"
            f"workdir: {self.deps.workdir}\n"
            "Enter submits · Ctrl+J newline · Ctrl+C interrupts · exit/quit to leave · /mode to switch\n"
        )
