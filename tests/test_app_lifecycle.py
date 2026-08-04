"""AgentApp 取消与退出生命周期回归测试。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.app.app import AgentApp
from src.events.types import SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore


class _RecordingUI:
    is_tty = True

    def __init__(self) -> None:
        self.cancel_count = 0
        self.wait_count = 0

    @contextmanager
    def watch_interrupt(self, _callback):
        yield

    def cancel_active_input(self) -> bool:
        self.cancel_count += 1
        return True

    async def wait_interactions_idle(self) -> None:
        self.wait_count += 1


class _RecordingBus:
    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.join_count = 0

    async def request_output(self, message: str) -> None:
        self.outputs.append(message)

    async def join(self) -> None:
        self.join_count += 1


class _BlockingAgent:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.finished.set()


class _FinishOnCancellationAgent(_BlockingAgent):
    async def run(self) -> None:
        self.started.set()
        try:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return
        finally:
            self.finished.set()


def _app(
    ui: _RecordingUI,
    event_bus: object,
    store: AgentViewStore | None = None,
) -> AgentApp:
    deps = SimpleNamespace(ui=ui, event_bus=event_bus)
    return AgentApp(deps, store or AgentViewStore(), output_router=object())


def test_outer_cancellation_propagates_after_work_task_stops() -> None:
    async def scenario() -> None:
        ui = _RecordingUI()
        event_bus = _RecordingBus()
        app = _app(ui, event_bus)
        agent = _FinishOnCancellationAgent()
        turn = asyncio.create_task(app._run_agent_turn(agent))  # type: ignore[arg-type]
        await agent.started.wait()

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert agent.finished.is_set()
        assert app._work_task is None
        assert event_bus.outputs == []
        assert ui.cancel_count == 0

    asyncio.run(scenario())


def test_work_task_cancellation_remains_a_turn_interrupt() -> None:
    async def scenario() -> None:
        ui = _RecordingUI()
        event_bus = _RecordingBus()
        app = _app(ui, event_bus)
        agent = _BlockingAgent()
        turn = asyncio.create_task(app._run_agent_turn(agent))  # type: ignore[arg-type]
        await agent.started.wait()

        assert app._cancel_current_work()
        assert await turn is None

        assert agent.finished.is_set()
        assert app._work_task is None
        assert event_bus.outputs == ["\n已中断当前任务。\n"]
        assert event_bus.join_count == 1
        assert ui.cancel_count == 1
        assert ui.wait_count == 1

    asyncio.run(scenario())


class _BrowsingBus:
    def __init__(self, block_on: str) -> None:
        self.block_on = block_on
        self.blocked = asyncio.Event()

    async def request_choice(self, *_args) -> str:
        if self.block_on == "choice":
            self.blocked.set()
            await asyncio.Event().wait()
        return "worker-0"

    async def request_transcript_view(self, _uuid: str) -> None:
        self.blocked.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("block_on", ["choice", "transcript"])
def test_agent_browser_propagates_outer_cancellation(block_on: str) -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="test",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="start",
        ))
        event_bus = _BrowsingBus(block_on)
        app = _app(_RecordingUI(), event_bus, store)
        browser = asyncio.create_task(app._browse_subagents())
        await event_bus.blocked.wait()

        browser.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser

    asyncio.run(scenario())
