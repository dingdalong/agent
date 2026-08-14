"""Pointer wheel burst acceleration regressions."""

from __future__ import annotations

import asyncio

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from src.interfaces.tui.history_log import HistoryEntry, HistoryLog
from src.interfaces.tui.render_policy import TuiRenderPolicy
from src.interfaces.tui.widgets import PointerScrollMixin, TranscriptPanel


class _TranscriptApp(App[None]):
    CSS = """
    TranscriptPanel { height: 1fr; scrollbar-size: 0 0; }
    #transcript-content { height: auto; }
    """

    def compose(self) -> ComposeResult:
        with TranscriptPanel(id="transcript"):
            yield Static(
                "\n".join(f"transcript line {index}" for index in range(200)),
                id="transcript-content",
            )


class _NestedScrollApp(App[None]):
    CSS = """
    #parent { height: 12; scrollbar-size: 0 0; }
    #child { height: 6; scrollbar-size: 0 0; }
    #child-content, #parent-spacer { height: 30; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="parent"):
            with TranscriptPanel(id="child"):
                yield Static("child", id="child-content")
            yield Static("parent", id="parent-spacer")


class _HistoryApp(App[None]):
    CSS = "HistoryLog { height: 1fr; padding: 0 1; scrollbar-size: 0 0; }"

    def __init__(self, policy: TuiRenderPolicy | None = None) -> None:
        self.render_policy = policy or TuiRenderPolicy()
        super().__init__()

    def compose(self) -> ComposeResult:
        yield HistoryLog(id="history", policy=self.render_policy)


def _pointer_event(
    event_type: type[events.MouseScrollUp] | type[events.MouseScrollDown],
    widget: Widget,
    event_time: float,
    *,
    ctrl: bool = False,
    shift: bool = False,
) -> events.MouseScrollUp | events.MouseScrollDown:
    event = event_type(
        widget,
        0,
        0,
        0,
        0,
        0,
        shift,
        False,
        ctrl,
    )
    event.time = event_time
    return event


def _send_pointer_event(
    widget: PointerScrollMixin,
    event_type: type[events.MouseScrollUp] | type[events.MouseScrollDown],
    event_time: float,
    *,
    ctrl: bool = False,
    shift: bool = False,
) -> events.MouseScrollUp | events.MouseScrollDown:
    event = _pointer_event(
        event_type,
        widget,
        event_time,
        ctrl=ctrl,
        shift=shift,
    )
    if event_type is events.MouseScrollUp:
        widget._on_mouse_scroll_up(event)
    else:
        widget._on_mouse_scroll_down(event)
    return event


def test_transcript_burst_uses_event_time_and_caps_at_three_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app = _TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one(TranscriptPanel)
            await pilot.pause()
            assert transcript.max_scroll_y > 100
            app.scroll_sensitivity_y = 3
            transcript.scroll_to(y=20, animate=False, immediate=True)

            calls: list[dict[str, object]] = []
            scroll_to = transcript._scroll_to

            def track_scroll(*args, **kwargs) -> bool:
                calls.append(kwargs)
                return scroll_to(*args, **kwargs)

            monkeypatch.setattr(transcript, "_scroll_to", track_scroll)
            expected_positions = [23, 26, 29, 35, 41, 47, 56, 65]
            event_times = [0.00, 0.20, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26]
            for event_time, expected in zip(event_times, expected_positions):
                _send_pointer_event(
                    transcript,
                    events.MouseScrollDown,
                    event_time,
                )
                assert transcript.scroll_y == expected

            assert len(calls) == len(event_times)
            assert all(call["animate"] is False for call in calls)
            assert all(call["release_anchor"] is False for call in calls)

            _send_pointer_event(transcript, events.MouseScrollDown, 0.50)
            assert transcript.scroll_y == 68
            _send_pointer_event(transcript, events.MouseScrollUp, 0.51)
            assert transcript.scroll_y == 65

            _send_pointer_event(transcript, events.MouseScrollDown, 1.00)
            _send_pointer_event(transcript, events.MouseScrollDown, 1.01)
            _send_pointer_event(transcript, events.MouseScrollDown, 1.02)
            assert transcript.scroll_y == 77
            _send_pointer_event(transcript, events.MouseScrollDown, 0.90)
            assert transcript.scroll_y == 80

            stopped_at = transcript.scroll_y
            await pilot.pause(0.2)
            assert transcript.scroll_y == stopped_at

    asyncio.run(scenario())


@pytest.mark.parametrize(("ctrl", "shift"), [(True, False), (False, True)])
def test_pointer_modifiers_reset_burst_and_defer_to_textual(
    ctrl: bool,
    shift: bool,
) -> None:
    async def scenario() -> None:
        app = _TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one(TranscriptPanel)
            await pilot.pause()
            app.scroll_sensitivity_y = 3
            transcript.scroll_to(y=20, animate=False, immediate=True)
            _send_pointer_event(transcript, events.MouseScrollDown, 1.00)
            _send_pointer_event(transcript, events.MouseScrollDown, 1.01)
            _send_pointer_event(transcript, events.MouseScrollDown, 1.02)
            assert transcript.scroll_y == 32

            modified = _send_pointer_event(
                transcript,
                events.MouseScrollDown,
                1.03,
                ctrl=ctrl,
                shift=shift,
            )
            assert transcript.scroll_y == 32
            assert not modified._no_default_action
            assert not modified._stop_propagation

            _send_pointer_event(transcript, events.MouseScrollDown, 1.04)
            assert transcript.scroll_y == 35

    asyncio.run(scenario())


def test_pointer_boundary_resets_and_bubbles_to_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app = _NestedScrollApp()
        async with app.run_test(size=(80, 24)) as pilot:
            parent = app.query_one("#parent", VerticalScroll)
            child = app.query_one("#child", TranscriptPanel)
            await pilot.pause()
            assert parent.max_scroll_y > 0
            assert child.max_scroll_y > 0
            child.scroll_end(animate=False, immediate=True)

            calls = 0
            scroll_to = child._scroll_to

            def track_scroll(*args, **kwargs) -> bool:
                nonlocal calls
                calls += 1
                return scroll_to(*args, **kwargs)

            monkeypatch.setattr(child, "_scroll_to", track_scroll)
            event = _pointer_event(events.MouseScrollDown, child, 1.00)
            await child._on_message(event)
            await pilot.pause()

            assert calls == 1
            assert child.scroll_y == child.max_scroll_y
            assert parent.scroll_y == app.scroll_sensitivity_y

    asyncio.run(scenario())


def test_history_burst_preserves_tail_following_contract() -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_window_entries=200,
            history_window_chars=100_000,
            history_window_lines=500,
        )
        app = _HistoryApp(policy)
        async with app.run_test(size=(80, 20)) as pilot:
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(f"line {index}", spacing=0, id=f"line-{index}")
                for index in range(100)
            ])
            await history.wait_for_reflow()
            await pilot.pause()
            assert isinstance(history, PointerScrollMixin)
            assert history.scroll_y == history.max_scroll_y

            tail = history.max_scroll_y
            for event_time in (1.00, 1.01, 1.02):
                _send_pointer_event(history, events.MouseScrollUp, event_time)
            assert history.scroll_y == tail - 8
            assert not history.is_anchored

            upscrolled_y = history.scroll_y
            history.append_entry("while upscrolled", spacing=0, entry_id="upscrolled")
            assert history.scroll_y == upscrolled_y

            history.jump_to_tail()
            await history.wait_for_reflow()
            await pilot.pause()
            assert history.scroll_y == history.max_scroll_y

            tail = history.max_scroll_y
            for event_time in (2.00, 2.01, 2.02):
                _send_pointer_event(history, events.MouseScrollUp, event_time)
            assert history.scroll_y == tail - 8
            for event_time in (2.03, 2.04, 2.05):
                _send_pointer_event(history, events.MouseScrollDown, event_time)
            assert history.scroll_y == history.max_scroll_y

            history.append_entry("followed tail", spacing=0, entry_id="followed")
            await history.wait_for_reflow()
            await pilot.pause()
            assert history.scroll_y == history.max_scroll_y

    asyncio.run(scenario())


def test_accelerated_history_edge_shifts_one_page_without_losing_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_page_entries=10,
            history_window_entries=30,
            history_window_chars=10_000,
            history_window_lines=100,
        )
        app = _HistoryApp(policy)
        async with app.run_test(size=(80, 12)) as pilot:
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(f"line {index}", spacing=0, id=f"line-{index}")
                for index in range(60)
            ])
            await history.wait_for_reflow()
            await pilot.pause()
            assert history.window_range == (30, 60)
            history.scroll_to(y=9, animate=False, immediate=True)

            shifts: list[str] = []
            shift_page = history._shift_page

            def track_shift(direction: str) -> None:
                shifts.append(direction)
                shift_page(direction)

            monkeypatch.setattr(history, "_shift_page", track_shift)
            for event_time in (1.00, 1.01, 1.02):
                _send_pointer_event(history, events.MouseScrollUp, event_time)
            expected_anchor = history._capture_viewport_anchor()
            assert expected_anchor.entry_id == "line-30"

            await pilot.pause()
            await history.wait_for_reflow()
            await pilot.pause()

            assert shifts == ["older"]
            assert history.window_range == (20, 50)
            assert history._capture_viewport_anchor() == expected_anchor

    asyncio.run(scenario())
