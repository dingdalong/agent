import asyncio
import logging
from dataclasses import dataclass, field

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
        consumer_task = asyncio.create_task(self._consume_events())
        await asyncio.sleep(0)

        await self.deps.event_bus.request_output("Agent 已启动，输入 'exit' 退出。\n")
        try:
            agent = Agent(
                agent_type = "总控",
                description = "入口",
                deps = self.deps,
            )
            history = []
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

                interrupted = await self._run_agent_turn(agent, user_input, history)
                if interrupted:
                    pending_input = user_input
                    continue
        finally:
            if self.deps.event_bus:
                self.deps.event_bus.close()
            if consumer_task:
                consumer_task.cancel()
                await asyncio.gather(consumer_task, return_exceptions=True)

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
        history: list[dict],
    ) -> bool:
        history_len = len(history)
        self._work_task = asyncio.create_task(agent.run(user_input, history))
        with self.deps.ui.watch_interrupt(self._request_interrupt):
            try:
                await self._work_task
                return False
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._handle_interrupted_turn(history, history_len)
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

    async def _handle_interrupted_turn(
        self,
        history: list[dict],
        history_len: int,
    ) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
        self._cancel_current_work()
        self._cancel_active_user_request()
        if self._work_task:
            await asyncio.gather(self._work_task, return_exceptions=True)
        del history[history_len:]
        await self.deps.event_bus.request_output("\n已中断当前任务。\n")
        await self.deps.event_bus.join()

    async def shutdown(self):
        pass
