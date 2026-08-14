"""TextualInterface 终端生命周期回归测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from rich.text import Text

from src.events.menu import ChoiceMenu, InputMenu, TranscriptView
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.textual_ui import TextualInterface, TuiTermination
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.app import AgentTuiApp
from src.interfaces.tui.dialogs import PendingInteractions
from src.interfaces.tui.history_journal import PlainHistoryJournal
from src.interfaces.tui.render_policy import TuiRenderPolicy


class _RecordingStream:
    def __init__(self) -> None:
        self.output = ""
        self.flush_count = 0

    def write(self, text: str) -> int:
        self.output += text
        return len(text)

    def flush(self) -> None:
        self.flush_count += 1


class _FailingStream:
    def __init__(self, *, fail_on_flush: bool) -> None:
        self.fail_on_flush = fail_on_flush

    def write(self, text: str) -> int:
        if not self.fail_on_flush:
            raise OSError("unavailable")
        return len(text)

    def flush(self) -> None:
        raise ValueError("closed")


def _interface(monkeypatch: pytest.MonkeyPatch, stream: object) -> TextualInterface:
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(sys, "__stderr__", stream)
    interface = TextualInterface(AgentViewStore())
    interface.is_tty = True
    return interface


def test_stop_restores_vscode_keyboard_protocol_and_flushes(monkeypatch) -> None:
    stream = _RecordingStream()
    interface = _interface(monkeypatch, stream)

    asyncio.run(interface.stop())

    assert stream.output == "\x1b[=0u"
    assert stream.flush_count == 1


def test_stop_recognizes_vscode_term_program_case_insensitively(monkeypatch) -> None:
    stream = _RecordingStream()
    interface = _interface(monkeypatch, stream)
    monkeypatch.setenv("TERM_PROGRAM", "VsCoDe")

    asyncio.run(interface.stop())

    assert stream.output == "\x1b[=0u"


@pytest.mark.parametrize(
    ("term_program", "is_tty"),
    [("Apple_Terminal", True), ("vscode", False)],
)
def test_stop_does_not_restore_outside_vscode_tty(
    monkeypatch,
    term_program: str,
    is_tty: bool,
) -> None:
    stream = _RecordingStream()
    monkeypatch.setenv("TERM_PROGRAM", term_program)
    monkeypatch.setattr(sys, "__stderr__", stream)
    interface = TextualInterface(AgentViewStore())
    interface.is_tty = is_tty

    asyncio.run(interface.stop())

    assert stream.output == ""
    assert stream.flush_count == 0


def test_stop_restores_after_app_task_failure(monkeypatch) -> None:
    async def scenario() -> None:
        stream = _RecordingStream()
        interface = _interface(monkeypatch, stream)
        interface._plain.stream = stream

        async def fail() -> None:
            raise RuntimeError("app failed")

        interface._app_task = asyncio.create_task(fail())
        await interface.stop()

        assert stream.output == "\x1b[=0u"
        assert stream.flush_count == 1

    asyncio.run(scenario())


def test_stop_restores_when_shutdown_is_cancelled(monkeypatch) -> None:
    class CancelledApp:
        async def shutdown_ui(self) -> None:
            raise asyncio.CancelledError

    async def scenario() -> None:
        stream = _RecordingStream()
        interface = _interface(monkeypatch, stream)
        interface._app = CancelledApp()  # type: ignore[assignment]
        app_task = asyncio.create_task(asyncio.sleep(60))
        interface._app_task = app_task
        with pytest.raises(asyncio.CancelledError):
            await interface.stop()
        app_task.cancel()
        await asyncio.gather(app_task, return_exceptions=True)

        assert stream.output == "\x1b[=0u"
        assert stream.flush_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stream",
    [None, _FailingStream(fail_on_flush=False), _FailingStream(fail_on_flush=True)],
)
def test_stop_ignores_unavailable_stderr(monkeypatch, stream) -> None:
    interface = _interface(monkeypatch, stream)

    asyncio.run(interface.stop())


def test_cancel_after_app_failure_uses_non_rendering_cleanup(monkeypatch) -> None:
    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        app = AgentTuiApp(
            interface.agent_view_store,
            [],
            TurnClock(),
            lambda: None,
            lambda: False,
            lambda: None,
            native_clipboard=False,
        )
        future = asyncio.get_running_loop().create_future()
        request = InputMenu(timestamp=1.0, source="test", future=future)
        app.coordinator.active = request
        interface._app = app

        async def fail() -> None:
            raise RuntimeError("app failed")

        interface._app_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        assert interface.cancel_active_input()
        assert future.cancelled()
        assert not await interface._invoke(lambda: 1)
        await interface.stop()

    asyncio.run(scenario())


def test_dead_app_rejects_new_request_without_hanging(monkeypatch) -> None:
    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        interface._app_task = asyncio.create_task(asyncio.sleep(0))
        await interface._app_task
        future = asyncio.get_running_loop().create_future()
        request = InputMenu(timestamp=1.0, source="test", future=future)
        assert not await interface._accept_ui_request(request)
        assert not future.done()

    asyncio.run(scenario())


def test_wait_interactions_idle_drains_scheduled_cancel(monkeypatch) -> None:
    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        app = AgentTuiApp(
            interface.agent_view_store,
            [],
            TurnClock(),
            lambda: None,
            lambda: False,
            lambda: None,
            native_clipboard=False,
        )
        async with app.run_test(size=(80, 24)):
            interface._app = app
            interface._app_task = asyncio.create_task(asyncio.sleep(60))
            future = asyncio.get_running_loop().create_future()
            request = InputMenu(timestamp=1.0, source="test", future=future)
            await app.coordinator.submit(request)
            assert interface.cancel_active_input()
            await interface.wait_interactions_idle()
            assert future.cancelled()
            interface._app_task.cancel()
            await asyncio.gather(interface._app_task, return_exceptions=True)
            interface._app = None

    asyncio.run(scenario())


def test_textual_return_code_detects_swallowed_fatal_error(monkeypatch) -> None:
    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        interface._app_task = asyncio.create_task(asyncio.sleep(0))
        await interface._app_task
        fallen_back = asyncio.Event()

        async def fallback(_app, termination) -> None:
            assert isinstance(termination, TuiTermination)
            assert termination.kind == "textual_fatal"
            assert isinstance(termination.error, RuntimeError)
            fallen_back.set()

        interface._fallback_to_plain = fallback  # type: ignore[method-assign]
        app = SimpleNamespace(
            return_code=1,
            fatal_error=RuntimeError("swallowed fatal"),
        )
        interface._on_app_task_done(interface._app_task, app)
        await asyncio.wait_for(fallen_back.wait(), timeout=1)

    asyncio.run(scenario())


def test_unexpected_return_has_one_structured_record_without_fake_error(
    monkeypatch,
    tmp_path,
) -> None:
    async def scenario() -> None:
        interface = TextualInterface(
            AgentViewStore(),
            diagnostic_dir=tmp_path,
        )
        interface.is_tty = True
        task = asyncio.create_task(asyncio.sleep(0))
        interface._app_task = task
        await task
        fallen_back = asyncio.Event()

        async def fallback(_app, termination) -> None:
            assert termination.kind == "unexpected_return"
            assert termination.error is None
            fallen_back.set()

        interface._fallback_to_plain = fallback  # type: ignore[method-assign]
        app = SimpleNamespace(
            return_code=0,
            fatal_error=None,
            _exception=None,
            _exit=False,
            is_running=False,
        )
        interface._on_app_task_done(task, app)
        interface._on_app_task_done(task, app)
        await asyncio.wait_for(fallen_back.wait(), timeout=1)
        await asyncio.gather(interface._fallback_task, return_exceptions=True)
        await interface.stop()

    asyncio.run(scenario())
    entries = [
        json.loads(line)
        for line in (tmp_path / "tui.jsonl").read_text().splitlines()
    ]
    terminated = [entry for entry in entries if entry["event"] == "app_terminated"]
    assert len(terminated) == 1
    assert terminated[0]["termination_kind"] == "unexpected_return"
    assert "exception_type" not in terminated[0]
    assert "Textual app ended unexpectedly" not in (tmp_path / "tui.jsonl").read_text()


def test_real_task_exception_has_one_diagnostic_record(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        interface = TextualInterface(
            AgentViewStore(),
            diagnostic_dir=tmp_path,
        )
        interface.is_tty = True

        async def fail() -> None:
            raise LookupError("real app failure")

        task = asyncio.create_task(fail())
        interface._app_task = task
        await asyncio.gather(task, return_exceptions=True)
        fallen_back = asyncio.Event()

        async def fallback(_app, termination) -> None:
            assert termination.kind == "task_exception"
            assert isinstance(termination.error, LookupError)
            fallen_back.set()

        interface._fallback_to_plain = fallback  # type: ignore[method-assign]
        app = SimpleNamespace(
            return_code=0,
            fatal_error=None,
            _exception=None,
            _exit=False,
            is_running=False,
        )
        interface._on_app_task_done(task, app)
        await asyncio.wait_for(fallen_back.wait(), timeout=1)
        await asyncio.gather(interface._fallback_task, return_exceptions=True)
        await interface.stop()

    asyncio.run(scenario())
    entries = [
        json.loads(line)
        for line in (tmp_path / "tui.jsonl").read_text().splitlines()
    ]
    terminated = [entry for entry in entries if entry["event"] == "app_terminated"]
    assert len(terminated) == 1
    assert terminated[0]["exception_type"] == "LookupError"
    assert terminated[0]["termination_kind"] == "task_exception"


def test_history_journal_preserves_plain_rich_markdown_and_stream_text() -> None:
    journal = PlainHistoryJournal()
    journal.append_entry(Text("Rich 输出", style="bold red"))
    journal.append_entry("**Markdown 原文**")
    journal.start_stream("response")
    journal.append_stream("response", "流式")
    journal.append_stream("response", "输出")
    journal.end_stream("response")

    assert journal.snapshot() == "Rich 输出\n**Markdown 原文**\n流式输出\n"


def test_history_journal_bounds_completed_and_active_text_together() -> None:
    policy = TuiRenderPolicy(journal_chars=20)
    journal = PlainHistoryJournal(policy)
    journal.append_entry("older-a")
    journal.append_entry("older-b")
    journal.start_stream("response")
    journal.append_stream("response", "1234567890")
    journal.append_stream("response", "abcdefghijklmno")

    active_snapshot = journal.snapshot()
    assert journal._entry_chars + journal._active_chars <= policy.journal_chars
    assert journal._active_buffer.tell() <= policy.journal_chars
    assert active_snapshot.startswith("[较早历史未回放]\n")
    assert "older-a" not in active_snapshot
    assert "older-b" not in active_snapshot

    for _ in range(50_000):
        journal.append_stream("response", "x")
    assert journal._active_buffer.tell() == policy.journal_chars
    assert len(journal._active_buffer._blocks) <= 2

    journal.end_stream("response")

    assert journal._entry_chars <= policy.journal_chars
    assert journal.snapshot().startswith("[较早历史未回放]\n")


def test_plain_fallback_replays_history_once(monkeypatch) -> None:
    async def scenario() -> None:
        stream = _RecordingStream()
        interface = _interface(monkeypatch, _RecordingStream())
        interface._plain.stream = stream
        interface.history_journal.append_entry(Text("Rich 历史", style="bold"))
        interface.history_journal.append_entry("**Markdown 历史**")
        termination = TuiTermination(
            kind="task_exception",
            error=RuntimeError("boom"),
            task_error=RuntimeError("boom"),
            fatal_error=None,
            internal_exception=None,
            return_code=0,
            exit_requested=False,
            app_running=False,
        )
        await interface._fallback_to_plain(None, termination)
        await interface._fallback_to_plain(None, termination)

        assert not interface.is_tty
        assert stream.output.count("Rich 历史") == 1
        assert stream.output.count("**Markdown 历史**") == 1
        assert stream.output.count("已切换到文字模式") == 1

    asyncio.run(scenario())


def test_pending_interactions_restart_in_plain_mode(monkeypatch) -> None:
    async def scenario() -> None:
        answers = iter(["2", "重新输入"])

        async def reader(_prompt: str) -> str:
            return next(answers)

        interface = _interface(monkeypatch, _RecordingStream())
        interface.is_tty = False
        interface._plain.reader = reader
        active = ChoiceMenu(
            timestamp=1.0,
            source="test",
            prompt="选择",
            options=[("a", "A"), ("b", "B")],
            future=asyncio.get_running_loop().create_future(),
        )
        queued = InputMenu(
            timestamp=2.0,
            source="test",
            prompt="输入",
            default="旧草稿",
            future=asyncio.get_running_loop().create_future(),
        )
        view = TranscriptView(
            timestamp=3.0,
            source="test",
            uuid="worker-0",
            future=asyncio.get_running_loop().create_future(),
        )
        interface._pending_interactions = PendingInteractions(
            active=active,
            queue=[queued],
            view_request=view,
        )

        await interface._drain_pending_interactions()

        assert await active.future == "b"
        assert await queued.future == "重新输入"
        assert await view.future == ""
        assert interface._pending_interactions is None

    asyncio.run(scenario())


def test_new_request_waits_for_pending_plain_interactions(monkeypatch) -> None:
    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        interface.is_tty = False
        interface._ui_ready.clear()
        release = asyncio.Event()

        async def fallback() -> None:
            await release.wait()
            interface._ui_ready.set()

        interface._fallback_task = asyncio.create_task(fallback())
        request = InputMenu(
            timestamp=1.0,
            source="test",
            future=asyncio.get_running_loop().create_future(),
        )
        accepted = asyncio.create_task(interface._accept_ui_request(request))
        await asyncio.sleep(0)
        assert not accepted.done()

        release.set()
        assert not await asyncio.wait_for(accepted, timeout=1)
        assert not request.future.done()

    asyncio.run(scenario())


def test_inflight_invoke_finishes_when_app_exits(monkeypatch) -> None:
    class FakeCoordinator:
        modal_active = False
        active = None
        queue = []
        view_request = None

        def detach_for_fallback(self) -> PendingInteractions:
            return PendingInteractions(None, [], None)

        def cancel_all(self, *, render: bool = True) -> bool:
            del render
            return False

    class FakeApp:
        return_code = 0
        fatal_error = None
        _exception = None
        _exit = False
        is_running = False
        viewing_agent_id = None
        _response_stream = None
        _thinking_stream = None
        coordinator = FakeCoordinator()

        async def invoke(self, _callback) -> None:
            await asyncio.Future()

    async def scenario() -> None:
        interface = _interface(monkeypatch, _RecordingStream())
        interface._plain.stream = _RecordingStream()
        app = FakeApp()
        release = asyncio.Event()

        async def run_app() -> None:
            await release.wait()

        app_task = asyncio.create_task(run_app())
        app_task.add_done_callback(
            lambda finished: interface._on_app_task_done(finished, app)  # type: ignore[arg-type]
        )
        interface._app = app  # type: ignore[assignment]
        interface._app_task = app_task
        interface._ui_ready.set()
        assert interface._ui_ready.is_set()
        invocation = asyncio.create_task(interface._invoke(lambda: None))
        await asyncio.sleep(0)
        release.set()

        assert not await asyncio.wait_for(invocation, timeout=1)
        assert not interface.is_tty

    asyncio.run(scenario())


def test_app_failure_does_not_start_second_textual_app(monkeypatch) -> None:
    created: list[object] = []
    crash = asyncio.Event()

    class FakeCoordinator:
        modal_active = False
        active = None
        queue = []
        view_request = None

        def detach_for_fallback(self) -> PendingInteractions:
            return PendingInteractions(None, [], None)

        def cancel_all(self, *, render: bool = True) -> bool:
            del render
            return False

    class FakeApp:
        return_code = 0
        fatal_error = RuntimeError("fatal")
        _exception = None
        _exit = False
        is_running = False
        viewing_agent_id = None
        _response_stream = None
        _thinking_stream = None

        def __init__(self, *_args, **_kwargs) -> None:
            created.append(self)
            self.ready = asyncio.Event()
            self.coordinator = FakeCoordinator()

        async def run_async(self) -> None:
            self.ready.set()
            await crash.wait()

    async def scenario() -> None:
        monkeypatch.setattr("src.interfaces.textual_ui.AgentTuiApp", FakeApp)
        interface = _interface(monkeypatch, _RecordingStream())
        interface._plain.stream = _RecordingStream()
        await interface.start()
        crash.set()
        await asyncio.gather(interface._app_task, return_exceptions=True)
        await asyncio.wait_for(interface._fallback_task, timeout=1)

        assert len(created) == 1
        assert not interface.is_tty
        await interface.stop()

    asyncio.run(scenario())
