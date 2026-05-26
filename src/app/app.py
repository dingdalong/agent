import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.agent import Agent, AgentDeps
from src.events import NoEventSubscribers
from src.events.types import InterruptRequested, UserInputRequest

logger = logging.getLogger(__name__)

@dataclass
class AgentApp:
    deps: AgentDeps = field(repr=False)
    _work_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _active_user_request: UserInputRequest | None = field(default=None, init=False, repr=False)

    async def run(self) -> None:
        consumer_task = None
        try:
            await self.deps.ui.start()
            consumer_task = asyncio.create_task(self._consume_events())
            await asyncio.sleep(0)

            await self.deps.event_bus.request_output(self._startup_banner())
            if not self.deps.session_id:
                self.deps.session_id = str(uuid.uuid4())
            await self._run_session_start_hooks()
            agent = Agent(
                agent_type = "总控",
                description = "入口",
                deps = self.deps,
            )
            pending_input = ""
            while True:
                try:
                    user_input = await self.deps.event_bus.request_input(
                        "\n\n你: ",
                        default=pending_input,
                    )
                    pending_input = ""
                except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                    break
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                if user_input.strip().lower() == "/clear":
                    for attr in ("memory_mgr", "tools_mgr", "permission_mgr",
                                 "config_mgr", "hooks_mgr"):
                        mgr = getattr(self.deps, attr, None)
                        if mgr is not None and hasattr(mgr, "reload"):
                            mgr.reload()
                    self.deps.session_context.clear()
                    agent = Agent(
                        agent_type="总控",
                        description="入口",
                        deps=self.deps,
                    )
                    await self._run_session_start_hooks(source="clear")
                    await self.deps.event_bus.request_output("上下文已清理，所有组件已重载。\n")
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
        async for event in self.deps.event_bus.subscribe():
            if isinstance(event, InterruptRequested):
                self._handle_interrupt_requested()
                continue
            if isinstance(event, UserInputRequest):
                await self._dispatch_user_request(event)
                continue
            await self.deps.ui.on_event(event)

    async def _dispatch_user_request(self, event: UserInputRequest) -> None:
        self._active_user_request = event
        with self.deps.ui.watch_interrupt(self._request_interrupt):
            await self.deps.ui.on_event(event)
        self._clear_completed_user_request(event)

    async def _run_agent_turn(
        self,
        agent: Agent,
        user_input: str,
    ) -> bool:
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
        return (
            "Agent workbench ready\n"
            f"model: {model}\n"
            f"workdir: {Path.cwd()}\n"
            "Enter submits · Ctrl+J newline · Ctrl+C interrupts · exit/quit to leave\n"
        )
