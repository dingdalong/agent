"""AgentApp 会话、取消与退出生命周期回归测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.agent import Agent
from src.agent.spec import AgentSpec
from src.agent.states import RunResult
from src.app.app import AgentApp
from src.common.session_state import SessionState
from src.agent import SessionContext
from src.events.types import SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore
from src.mode import RunMode


class _RecordingUI:
    is_tty = True

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.cancel_count = 0
        self.finish_count = 0
        self.wait_count = 0

    @contextmanager
    def watch_interrupt(self, _callback):
        yield

    def cancel_active_input(self) -> bool:
        self.cancel_count += 1
        self.order.append("cancel")
        return True

    async def finish_interrupted_turn(self) -> None:
        self.finish_count += 1
        self.order.append("finish_interrupted")

    async def wait_interactions_idle(self) -> None:
        self.wait_count += 1
        self.order.append("wait")


class _RecordingBus:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.outputs: list[str] = []
        self.join_count = 0

    async def request_output(self, message: str) -> None:
        self.outputs.append(message)
        self.order.append("notice")

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


class _SessionUI:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def start(self) -> None:
        self.order.append("ui_start")

    async def stop(self) -> None:
        self.order.append("ui_stop")

    @asynccontextmanager
    async def reset_session_interactions(self):
        self.order.append("ui_gate")
        yield

    async def replace_session_state(self, _state: SessionState) -> None:
        self.order.append("replace")


class _SessionBus:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.outputs: list[object] = []

    @contextmanager
    def reject_ui_requests(self):
        self.order.append("bus_gate")
        yield

    async def join(self) -> None:
        self.order.append("join")

    async def request_output(self, content: object) -> None:
        self.outputs.append(content)
        self.order.append("banner")

    def close(self) -> None:
        self.order.append("close")


class _SessionHooks:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def reload(self) -> None:
        self.order.append("hooks_reload")

    async def run_event(self, event: str, source: str, _payload: dict, **_kwargs):
        self.order.append(f"hook:{event}:{source}")
        return SimpleNamespace(additional_context=[])


class _ReloadingRoleMgr:
    role_name = "old-role"
    manifest = SimpleNamespace(
        model="old-alias",
        reasoning_effort=None,
        description="old description",
        start_in_plan_mode=False,
        features=None,
    )

    def __init__(self, order: list[str]) -> None:
        self.order = order

    def reload(self) -> None:
        self.order.append("role_reload")
        self.role_name = "reloaded-role"
        self.manifest = SimpleNamespace(
            model="reloaded-alias",
            reasoning_effort=None,
            description="reloaded description",
            start_in_plan_mode=True,
            features=None,
        )


class _ReloadingLLMMgr:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.model = "old-model"

    def reconfigure(self) -> None:
        self.order.append("llm_reconfigure")
        self.model = "reloaded-model"

    def get(self, _model: str | None):
        return SimpleNamespace(model=self.model, reasoning_effort="high")


def _session_app(order: list[str]) -> tuple[AgentApp, _SessionBus]:
    bus = _SessionBus(order)
    deps = SimpleNamespace(
        ui=_SessionUI(order),
        event_bus=bus,
        hooks_mgr=_SessionHooks(order),
        trust_gate=None,
        session=SessionContext(state=SessionState()),
        session_mgr=None,
        config_mgr=SimpleNamespace(project_trusted=False, environment={}),
        data_guard=None,
        global_dir=None,
        workdir="/workspace",
        mcp_mgr=None,
        memory_mgr=None,
        context_mgr=None,
        tools_mgr=None,
        plugin_mgr=None,
        plan_mgr=None,
        role_mgr=_ReloadingRoleMgr(order),
        llm_mgr=_ReloadingLLMMgr(order),
        command_mgr=None,
    )
    output_router = SimpleNamespace(
        bind_session_state=lambda _state: order.append("bind_state")
    )
    return AgentApp(deps, AgentViewStore(), output_router), bus


def test_run_outputs_startup_banner_once() -> None:
    order: list[str] = []
    app, bus = _session_app(order)
    new_agent = SimpleNamespace(uuid="startup-agent", agent_type="main")

    async def consume_events() -> None:
        await asyncio.Event().wait()

    async def finish_turn(_agent) -> RunResult:
        return RunResult(exit_requested=True)

    with (
        patch("src.app.app.Agent.from_manifest", return_value=new_agent),
        patch.object(AgentApp, "_install_plan_mode_controller"),
        patch.object(app, "_consume_events", new=consume_events),
        patch.object(app, "_run_agent_turn", new=finish_turn),
    ):
        asyncio.run(app.run())

    assert len(bus.outputs) == 1
    banner = bus.outputs[0].plain
    assert "old-model high" in banner
    assert "old-role  old description" in banner
    assert "Plan  inactive" in banner
    assert order.index("replace") < order.index("hook:SessionStart:startup")
    assert order.index("hook:SessionStart:startup") < order.index("banner")
    assert order.index("banner") < order.index("join", order.index("banner"))


def test_clear_outputs_one_banner_with_reloaded_session_details() -> None:
    order: list[str] = []
    app, bus = _session_app(order)
    new_agent = SimpleNamespace(uuid="clear-agent", agent_type="main")

    with (
        patch("src.app.app.Agent.from_manifest", return_value=new_agent),
        patch.object(AgentApp, "_install_plan_mode_controller"),
    ):
        assert asyncio.run(app.reset_session(source="clear")) is new_agent

    assert len(bus.outputs) == 1
    banner = bus.outputs[0].plain
    assert "reloaded-model high" in banner
    assert "reloaded-role  reloaded description" in banner
    assert "Plan  active" in banner
    assert "/workspace" in banner
    assert order.index("replace") < order.index("hook:SessionStart:clear")
    assert order.index("hook:SessionStart:clear") < order.index("banner")
    assert order.index("banner") < order.index("join", order.index("banner"))


def test_reset_session_refreshes_env_baseline_and_rebinds_ledger(tmp_path) -> None:
    """会话重置时重算环境基线、清空共享账本并绑定新会话 ID。

    环境基线只在这一处采集（本方法同时覆盖 startup 与 /clear），采集后才创建新
    Agent，因此新 PromptMgr 自然读到新值。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    from src.mgr.context_mgr import ContextMgr

    order: list[str] = []
    app, _bus = _session_app(order)
    ledger = ContextMgr(workdir=tmp_path)
    ledger.bind_session("old-session")
    ledger.add(
        kind="note", topic="旧会话遗留",
        content="上一个会话里核实过的结论，重置后不应再出现在账本里。",
        author="explore",
    )
    app.deps.context_mgr = ledger
    app.deps.workdir = tmp_path
    new_agent = SimpleNamespace(uuid="clear-agent", agent_type="main")

    with (
        patch("src.app.app.Agent.from_manifest", return_value=new_agent),
        patch.object(AgentApp, "_install_plan_mode_controller"),
    ):
        asyncio.run(app.reset_session(source="clear"))

    assert app.deps.session.env_baseline
    assert ledger.session_id == app.deps.session.id
    assert ledger._entries == []
    assert ledger.digest() == ""


def _app(
    ui: _RecordingUI,
    event_bus: object,
    store: AgentViewStore | None = None,
) -> AgentApp:
    deps = SimpleNamespace(
        ui=ui, event_bus=event_bus, session=SessionContext(),
    )
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
        assert event_bus.outputs == ["\n已中断本轮任务，已产出的内容已保留。\n"]
        assert event_bus.join_count == 2
        assert ui.cancel_count == 1
        assert ui.finish_count == 1
        assert ui.wait_count == 1

    asyncio.run(scenario())


def test_interrupt_saves_session_before_user_notice() -> None:
    """中断提示必须晚于首次强制落盘，且界面收尾发生在落盘之后。"""

    class Sessions:
        def __init__(self, order: list[str]) -> None:
            self.order = order

        def append_batch(self, _session_id, _payload, resync=None) -> bool:
            self.order.append("save")
            return True

    async def scenario() -> None:
        order: list[str] = []
        ui = _RecordingUI(order)
        event_bus = _RecordingBus(order)
        app = _app(ui, event_bus)
        app.deps.session_mgr = Sessions(order)
        state = SessionState()
        state.append_user("中断前", {"role": "user", "content": "中断前"})
        app.deps.session.state = state
        app.deps.session.id = "session"

        await app._handle_interrupted_turn()

        assert order == ["cancel", "save", "finish_interrupted", "notice", "wait"]

    asyncio.run(scenario())


def test_interrupt_save_failure_warns_and_keeps_pending_delta() -> None:
    """落盘失败时如实提示，并保留增量等待下次检查点重试。"""

    class FailingSessions:
        def __init__(self) -> None:
            self.calls = 0

        def append_batch(self, _session_id, _payload, resync=None) -> bool:
            self.calls += 1
            return False

    async def scenario() -> None:
        ui = _RecordingUI()
        event_bus = _RecordingBus()
        app = _app(ui, event_bus)
        sessions = FailingSessions()
        app.deps.session_mgr = sessions
        state = SessionState()
        state.append_user("中断前", {"role": "user", "content": "中断前"})
        app.deps.session.state = state
        app.deps.session.id = "session"

        await app._handle_interrupted_turn()

        assert sessions.calls == 2
        assert event_bus.outputs == [
            "\n已中断本轮任务，已产出的内容已保留。\n",
            "\n会话保存失败，将在下次检查点重试。\n",
        ]
        assert state.checkpoint_due(force=True) is True

    asyncio.run(scenario())


def test_slow_interrupt_save_does_not_block_event_loop_and_is_awaited() -> None:
    class SlowSessions:
        def __init__(self) -> None:
            self.saved = False
            self.thread_id: int | None = None

        def append_batch(self, _session_id, _payload, resync=None) -> bool:
            self.thread_id = threading.get_ident()
            time.sleep(0.12)
            self.saved = True
            return True

    async def scenario() -> None:
        ui = _RecordingUI()
        event_bus = _RecordingBus()
        app = _app(ui, event_bus)
        sessions = SlowSessions()
        app.deps.session_mgr = sessions
        state = SessionState()
        state.append_user("中断前", {"role": "user", "content": "中断前"})
        app.deps.session.state = state
        app.deps.session.id = "session"
        main_thread = threading.get_ident()
        beats = 0
        running = True

        async def heartbeat() -> None:
            nonlocal beats
            while running:
                beats += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        started = time.monotonic()
        await app._handle_interrupted_turn()
        elapsed = time.monotonic() - started
        running = False
        await heartbeat_task

        assert sessions.saved
        assert sessions.thread_id != main_thread
        assert elapsed >= 0.1
        assert beats >= 5

    asyncio.run(scenario())


def test_agent_round_persistence_serializes_in_worker_thread() -> None:
    class SlowSessions:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.thread_ids: list[int] = []

        def append_batch(self, _session_id, _payload, resync=None) -> None:
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.05)
            self.calls.append("state")

        def get_metadata(self, _session_id):
            self.thread_ids.append(threading.get_ident())
            return None

        def save_metadata(self, _session_id, **_kwargs) -> None:
            self.thread_ids.append(threading.get_ident())
            self.calls.append("metadata")

    async def scenario() -> None:
        sessions = SlowSessions()
        state = SessionState()
        state.append_context({"role": "user", "content": "hello"})
        agent = object.__new__(Agent)
        agent.spec = AgentSpec(agent_type="main", description="")
        agent.deps = SimpleNamespace(
            session_mgr=sessions,
            session=SessionContext(id="session", state=state),
        )
        agent.history = [{"role": "user", "content": "hello"}]
        agent.mode = RunMode.EXECUTE
        main_thread = threading.get_ident()
        beats = 0
        running = True

        async def heartbeat() -> None:
            nonlocal beats
            while running:
                beats += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        await agent._commit_session("hello")
        running = False
        await heartbeat_task

        assert sessions.calls == ["state", "metadata"]
        assert sessions.thread_ids
        assert all(thread_id != main_thread for thread_id in sessions.thread_ids)
        assert beats >= 3

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
        from src.commands.builtin.agents import agents as agents_run
        from src.commands.context import CommandContext

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
        ctx = CommandContext(deps=app.deps, app=app)
        browser = asyncio.create_task(agents_run(ctx, []))
        await event_bus.blocked.wait()

        browser.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser

    asyncio.run(scenario())
