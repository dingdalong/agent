"""HistoryLog 单控件历史、流式与重排回归测试。"""

from __future__ import annotations

import asyncio

from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.selection import Selection

from src.interfaces.tui.history_log import HistoryEntry, HistoryLog


class _HistoryApp(App[None]):
    CSS = "HistoryLog { height: 1fr; padding: 0 1; scrollbar-size: 0 0; }"

    def compose(self) -> ComposeResult:
        yield HistoryLog(id="history")


def test_bulk_markdown_and_plain_replay_keeps_constant_dom() -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(100, 32)) as pilot:
            history = app.query_one(HistoryLog)
            baseline_dom = len(list(app.walk_children()))
            markdown_entries = [
                HistoryEntry(
                    f"## section {index}\n\nparagraph {index}\n\n```python\nvalue = {index}\n```",
                    markdown=True,
                    id=f"markdown-{index}",
                )
                for index in range(300)
            ]
            history.replace_entries(markdown_entries)
            await history.wait_for_reflow()
            await pilot.pause()

            assert history.max_lines is None
            assert len(history.entries) == 300
            assert len(history.entry_ranges) == 300
            assert not history.children
            assert len(list(app.walk_children())) == baseline_dom
            assert history.entries[-1].content == markdown_entries[-1].content
            assert len(history.lines) > len(markdown_entries)

            plain_entries = [
                HistoryEntry(f"plain replay {index}", id=f"plain-{index}")
                for index in range(1_000)
            ]
            history.replace_entries(plain_entries)
            await history.wait_for_reflow()
            await pilot.pause()

            assert len(history.entries) == 1_000
            assert len(history.entry_ranges) == 1_000
            assert not history.children
            assert len(list(app.walk_children())) == baseline_dom
            assert [entry.content for entry in history.entries] == [
                entry.content for entry in plain_entries
            ]

    asyncio.run(scenario())


def test_empty_hydration_does_not_strand_eager_reflow_task() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            app = _HistoryApp()
            async with app.run_test(size=(100, 32)):
                history = app.query_one(HistoryLog)
                history.replace_entries([])
                await history.wait_for_reflow()

                assert history._reflow_task is None

                history.append_entry("welcome", entry_id="welcome")
                history.begin_stream("response", entry_id="response")
                history.append_stream("response", "final ")
                history.append_stream("response", "answer")
                history.end_stream("response")
                await history.wait_for_reflow()

                visible_lines = [line.text.rstrip() for line in history.lines]
                assert "welcome" in visible_lines
                assert "final answer" in visible_lines
                assert history._reflow_task is None
        finally:
            loop.set_task_factory(previous_factory)

    asyncio.run(scenario())


def test_fifty_thousand_deltas_use_one_coalesced_tail() -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(100, 32)):
            history = app.query_one(HistoryLog)
            history.begin_stream("response")
            for _ in range(50_000):
                history.append_stream("response", "x")

            buffer = history._streams["response"]
            assert len(history.entries) == 1
            assert history.active_stream_id == "response"
            assert len(buffer.chunks) == 50_000
            assert sum(map(len, buffer.chunks)) == 50_000
            assert history.stream_flush_count == 0
            assert history.stream_merge_count == 49_999

            final = history.end_stream("response")
            assert final == "x" * 50_000
            assert history.entries[0].content == final
            assert history.active_stream_id is None
            assert history._stream_timer is None
            assert not history.children

    asyncio.run(scenario())


def test_selection_reads_only_selected_rich_lines() -> None:
    class CountingLines(list):
        def __init__(self, values) -> None:
            super().__init__(values)
            self.reads = 0

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(80, 24)):
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(f"line {index}", id=f"line-{index}")
                for index in range(1_000)
            ])
            await history.wait_for_reflow()
            start, _end = history.entry_ranges["line-500"]
            counting = CountingLines(history.lines)
            history.lines = counting

            selected, ending = history.get_selection(
                Selection(Offset(0, start), Offset(len("line 500"), start))
            )

            assert selected == "line 500"
            assert ending == "\n"
            assert counting.reads <= 2

    asyncio.run(scenario())


def test_mouse_drag_selects_only_requested_history_text() -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(80, 24)) as pilot:
            history = app.query_one(HistoryLog)
            history.append_entry("prefix selected history suffix", spacing=0)
            await history.wait_for_reflow()
            await pilot.pause()

            target = "selected history"
            start_x = len("prefix ")
            end_x = start_x + len(target)
            content = history.content_region
            widget_offset = (
                content.x - history.region.x + start_x,
                content.y - history.region.y,
            )
            await pilot.mouse_down(history, offset=widget_offset)
            end = Offset(content.x + end_x - 1, content.y)
            app.mouse_position = end
            app.screen._forward_event(events.MouseMove(
                history,
                *end,
                end_x - start_x,
                0,
                1,
                False,
                False,
                False,
                screen_x=end.x,
                screen_y=end.y,
            ))
            await pilot.pause()

            assert app.screen.selections == {
                history: Selection(Offset(start_x, 0), Offset(end_x, 0))
            }
            assert app.screen.get_selected_text() == target

            await pilot.mouse_up(offset=end)
            assert app.screen.get_selected_text() == target

    asyncio.run(scenario())


def test_follow_tail_upscroll_and_resize_restore_entry_anchor() -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(100, 30)) as pilot:
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(
                    f"entry {index}: " + "content " * 12,
                    id=f"entry-{index}",
                )
                for index in range(120)
            ])
            await history.wait_for_reflow()
            await pilot.pause()
            assert history.scroll_y == history.max_scroll_y

            history.append_entry("followed", entry_id="followed")
            assert history.scroll_y == history.max_scroll_y

            start, end = history.entry_ranges["entry-40"]
            line_offset = min(1, end - start - 1)
            history.scroll_to(y=start + line_offset, animate=False, immediate=True)
            old_scroll_y = history.scroll_y
            history.append_entry("while upscrolled", entry_id="upscrolled")
            assert history.scroll_y == old_scroll_y

            await pilot.resize_terminal(52, 30)
            await history.wait_for_reflow()
            await pilot.pause()
            new_start, new_end = history.entry_ranges["entry-40"]
            assert history.scroll_y == new_start + min(
                line_offset,
                new_end - new_start - 1,
            )

    asyncio.run(scenario())
