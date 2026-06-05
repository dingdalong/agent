"""AgentApp — 外层 REPL 和会话生命周期管理。"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.agent import Agent, AgentDeps
from src.agent.states import RunResult
from src.app.permission_mode_controller import PermissionModeController
from src.events.types import InterruptRequested, UserInputRequest

logger = logging.getLogger(__name__)


@dataclass
class AgentApp:
    """应用主循环，管理 REPL 交互和 Agent 执行。"""

    deps: AgentDeps = field(repr=False)
    _work_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _active_user_request: UserInputRequest | None = field(default=None, init=False, repr=False)
    _permission_mode_controller: PermissionModeController | None = field(default=None, init=False, repr=False)

    async def run(self) -> None:
        """启动主 REPL 循环。"""
        consumer_task = None
        try:
            await self.deps.ui.start()
            consumer_task = asyncio.create_task(self._consume_events())
            await asyncio.sleep(0)

            await self.deps.event_bus.request_output(self._startup_banner())
            if not self.deps.session_id:
                self.deps.session_id = str(uuid.uuid4())
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
        """消费事件总线上的事件并分发到 UI。"""
        async for event in self.deps.event_bus.subscribe():
            if isinstance(event, InterruptRequested):
                self._handle_interrupt_requested()
                continue
            if isinstance(event, UserInputRequest):
                await self._dispatch_user_request(event)
                continue
            await self.deps.ui.on_event(event)

    async def _dispatch_user_request(self, event: UserInputRequest) -> None:
        """分发用户输入请求到 UI 层。"""
        self._active_user_request = event
        with self.deps.ui.watch_interrupt(self._request_interrupt):
            await self.deps.ui.on_event(event)
        self._clear_completed_user_request(event)

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
        for attr in ("memory_mgr", "tools_mgr", "permission_mgr",
                     "config_mgr", "hooks_mgr", "plan_mgr"):
            mgr = getattr(self.deps, attr, None)
            if mgr is not None and hasattr(mgr, "reload"):
                mgr.reload()
        self.deps.session_context.clear()
        self._install_permission_mode_controller()
        if self._permission_mode_controller is not None:
            self._permission_mode_controller.notify_state_changed()
        agent = Agent(
            agent_type="总控",
            description="入口",
            deps=self.deps,
        )
        if self._permission_mode_controller is not None:
            self._permission_mode_controller.install_shortcut(agent)
        await self._run_session_start_hooks(source=source)
        return agent

    def _handle_interrupt_requested(self) -> None:
        if self._cancel_current_work():
            self._cancel_active_user_request()
            return
        self._cancel_active_user_request()

    def _cancel_current_work(self) -> bool:
        if self._work_task is None or self._work_task.done():
            return False
        self._work_task.cancel()
        return True

    def _cancel_active_user_request(self) -> bool:
        if self._active_user_request is None:
            return False
        self._active_user_request.cancel()
        self._active_user_request = None
        return True

    def _clear_completed_user_request(self, event: UserInputRequest) -> None:
        if self._active_user_request is not event:
            return
        if event.future is not None and event.future.done():
            self._active_user_request = None

    async def _handle_interrupted_turn(self) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
        self._cancel_current_work()
        self._cancel_active_user_request()
        if self._work_task:
            await asyncio.gather(self._work_task, return_exceptions=True)
        await self.deps.event_bus.request_output("\n已中断当前任务。\n")
        await self.deps.event_bus.join()

    async def shutdown(self):
        pass

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
            permission_mode = self.deps.permission_mgr.mode.value
        return (
            "Agent workbench ready\n"
            f"model: {model}\n"
            f"permission mode: {permission_mode}\n"
            f"workdir: {Path.cwd()}\n"
            "Enter submits · Ctrl+J newline · Ctrl+C interrupts · exit/quit to leave · /mode to switch\n"
        )
