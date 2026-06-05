"""AgentApp — 外层 REPL 和会话生命周期管理。"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.agent import Agent, AgentDeps
from src.app.permission_mode_controller import PermissionModeController
from src.events import NoEventSubscribers
from src.events.types import InterruptRequested, UserInputRequest

logger = logging.getLogger(__name__)


def parse_command(user_input: str) -> tuple[str, list[str]] | None:
    """尝试将用户输入解析为斜杠命令。

    按空格分割输入，第一个 token 必须以 "/" 开头才视为命令。
    命令名称转换为小写，参数保留原始大小写。

    Args:
        user_input: 用户原始输入字符串。

    Returns:
        解析成功返回 (命令名称, 参数列表) 元组，命令名称为小写且不含 "/" 前缀；
        输入不是斜杠命令时返回 None。
    """
    stripped = user_input.strip()
    if not stripped or not stripped.startswith("/"):
        return None
    parts = stripped.split()
    name = parts[0][1:].lower()
    args = parts[1:]
    return (name, args)


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
            pending_input = ""
            while True:
                try:
                    if self._permission_mode_controller is not None:
                        self._permission_mode_controller.install_shortcut(agent)
                    user_input = await self.deps.event_bus.request_input(
                        "\n\n你: ",
                        default=pending_input,
                    )
                    if self._permission_mode_controller is not None:
                        self._permission_mode_controller.clear_shortcut()
                    pending_input = ""
                except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                    break
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                cmd = parse_command(user_input)
                if cmd is not None:
                    cmd_name, cmd_args = cmd
                    if cmd_name == "clear":
                        agent = await self._reset_session(source="clear")
                        await self.deps.event_bus.request_output("上下文已清理，所有组件已重载。\n")
                        continue
                    if cmd_name == "mode":
                        if self._permission_mode_controller is not None:
                            await self._permission_mode_controller.prompt_selection(agent)
                        continue
                    if cmd_name == "plan":
                        await self._handle_plan_command(agent)
                        continue

                interrupted = await self._run_agent_turn(agent, user_input)
                if interrupted:
                    pending_input = user_input
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

    async def _run_agent_turn(
        self,
        agent: Agent,
        user_input: str,
    ) -> bool:
        """执行一轮 agent 对话。

        Args:
            agent: Agent 实例。
            user_input: 用户输入。

        Returns:
            是否被中断。
        """
        if self._permission_mode_controller is not None:
            self._permission_mode_controller.clear_shortcut()
        if self.deps.hooks_mgr is not None:
            hook_result = await self.deps.hooks_mgr.run_event(
                "UserPromptSubmit",
                user_input,
                {"prompt": user_input},
                session_id=self.deps.session_id,
                agent_id=str(agent.uuid),
                agent_type=agent.agent_type,
            )
            if hook_result.blocked:
                reason = hook_result.block_reason or "UserPromptSubmit hook blocked"
                await self.deps.event_bus.request_output(f"{reason}\n")
                return False
            if hook_result.additional_context:
                user_input = user_input + "\n\n" + "\n\n".join(str(item) for item in hook_result.additional_context)

        self._work_task = asyncio.create_task(agent.run(user_input))
        with self.deps.ui.watch_interrupt(self._request_interrupt):
            try:
                await self._work_task
                return False
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._handle_interrupted_turn()
                return True
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

    async def _handle_plan_command(self, agent: Agent) -> None:
        """处理 /plan 命令，进入计划模式。

        Args:
            agent: 当前 Agent 实例。
        """
        plan_mgr = self.deps.plan_mgr
        permission_mgr = self.deps.permission_mgr
        if permission_mgr is None or plan_mgr is None:
            return

        if not plan_mgr.enter_mode(permission_mgr):
            await self.deps.event_bus.request_output("已在计划模式中。\n")
            return

        agent.refresh_tools_schemas()
        if self._permission_mode_controller is not None:
            self._permission_mode_controller.notify_state_changed()
        await self.deps.event_bus.request_output("已进入计划模式。\n")

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
