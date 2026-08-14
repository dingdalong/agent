"""HistoryLog 单控件历史、流式与重排回归测试。"""

from __future__ import annotations

import asyncio

from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.selection import Selection

from src.interfaces.tui.history_log import HistoryEntry, HistoryLog
from src.interfaces.tui.render_policy import TuiRenderPolicy


class _HistoryApp(App[None]):
    CSS = "HistoryLog { height: 1fr; padding: 0 1; scrollbar-size: 0 0; }"

    def __init__(self, policy: TuiRenderPolicy | None = None) -> None:
        self.render_policy = policy or TuiRenderPolicy()
        super().__init__()

    def compose(self) -> ComposeResult:
        yield HistoryLog(id="history", policy=self.render_policy)


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
            assert history.window_range[1] == 300
            assert history.rendered_entry_count <= 250
            assert len(history.entry_ranges) == history.rendered_entry_count
            assert not history.children
            assert len(list(app.walk_children())) == baseline_dom
            assert history.entries[-1].content == markdown_entries[-1].content
            assert len(history.lines) > history.rendered_entry_count
            assert len(history.lines) <= 4_000

            plain_entries = [
                HistoryEntry(f"plain replay {index}", id=f"plain-{index}")
                for index in range(1_000)
            ]
            history.replace_entries(plain_entries)
            await history.wait_for_reflow()
            await pilot.pause()

            assert len(history.entries) == 1_000
            assert history.window_range[1] == 1_000
            assert history.rendered_entry_count <= 250
            assert len(history.entry_ranges) == history.rendered_entry_count
            assert len(history.lines) <= 4_000
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
            assert buffer.length == 50_000
            assert buffer.buffer.tell() == 50_000
            assert history.stream_flush_count == 0
            assert history.stream_merge_count == 49_999

            final = history.end_stream("response")
            assert final == "x" * 50_000
            assert history.entries[0].content == final
            assert history.active_stream_id is None
            assert history._stream_timer is None
            assert not history.children

    asyncio.run(scenario())


def test_stream_tail_refresh_does_not_render_stable_path_line(monkeypatch) -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(80, 12)) as pilot:
            history = app.query_one(HistoryLog)
            path = "/Users/example/project"
            history.append_entry(path, spacing=0, entry_id="path")
            history.begin_stream(
                "response",
                "tail",
                markdown=False,
                spacing=0,
                entry_id="response",
            )
            await pilot.pause()

            rendered_lines: list[int] = []
            render_line = history.render_line

            def record_render_line(y: int):
                rendered_lines.append(y)
                return render_line(y)

            monkeypatch.setattr(history, "render_line", record_render_line)
            history.append_stream("response", " update")
            history.end_stream("response")
            await pilot.pause()

            path_start, _ = history.entry_ranges["path"]
            response_start, _ = history.entry_ranges["response"]
            assert history.entries[0].content == path
            assert path_start - history.scroll_offset.y not in rendered_lines
            assert response_start - history.scroll_offset.y in rendered_lines

    asyncio.run(scenario())


def test_offscreen_stream_tail_update_does_not_render_visible_history(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        app = _HistoryApp()
        async with app.run_test(size=(80, 12)) as pilot:
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(f"stable line {index}", spacing=0, id=f"line-{index}")
                for index in range(80)
            ])
            await history.wait_for_reflow()
            history.begin_stream(
                "response",
                "tail",
                markdown=False,
                spacing=0,
                entry_id="response",
            )
            await pilot.pause()
            history.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()

            rendered_lines: list[int] = []
            render_line = history.render_line

            def record_render_line(y: int):
                rendered_lines.append(y)
                return render_line(y)

            monkeypatch.setattr(history, "render_line", record_render_line)
            history.append_stream("response", " update")
            history.end_stream("response")
            await pilot.pause()

            response_start, _ = history.entry_ranges["response"]
            assert response_start >= history.scrollable_content_region.height
            assert history.lines[response_start].text.rstrip() == "tail update"
            assert rendered_lines == []

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
            start, _end = history.entry_ranges["line-900"]
            counting = CountingLines(history.lines)
            history.lines = counting

            selected, ending = history.get_selection(
                Selection(Offset(0, start), Offset(len("line 900"), start))
            )

            assert selected == "line 900"
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


def test_scroll_edges_page_history_and_new_messages_do_not_move_old_page() -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_page_entries=10,
            history_window_entries=20,
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
            await history.wait_for_reflow()

            assert history.window_range == (40, 60)
            assert history.has_older
            assert not history.has_newer

            history.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            await history.wait_for_reflow()
            await pilot.pause()

            assert history.window_range == (30, 50)
            assert history.has_older
            assert history.has_newer
            assert "line-30" in history.entry_ranges
            assert "line-49" in history.entry_ranges
            old_scroll_y = history.scroll_y

            history.append_entry("new tail", spacing=0, entry_id="new-tail")

            assert len(history.entries) == 61
            assert history.window_range == (30, 50)
            assert history.scroll_y == old_scroll_y
            assert "new-tail" not in history.entry_ranges
            assert history.lines[-1].text.rstrip() == "↓ 较新历史 11 条"

            history.jump_to_tail()
            await history.wait_for_reflow()

            assert history.window_range == (41, 61)
            assert "new-tail" in history.entry_ranges
            assert not history.has_newer
            assert history.scroll_y == history.max_scroll_y

    asyncio.run(scenario())


def test_history_budgets_limit_window_and_project_oversized_entry() -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_window_entries=12,
            history_window_chars=90,
            history_window_lines=14,
            history_entry_chars=60,
            history_entry_source_lines=6,
        )
        app = _HistoryApp(policy)
        async with app.run_test(size=(80, 20)):
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry("x" * 20, spacing=0, id=f"entry-{index}")
                for index in range(30)
            ])
            await history.wait_for_reflow()

            visible = history.entries[slice(*history.window_range)]
            assert history.rendered_entry_count <= policy.history_window_entries
            assert sum(len(entry.content) for entry in visible) <= policy.history_window_chars
            assert len(history.lines) <= policy.history_window_lines

            source = "\n".join(
                f"source-{index:02d}-" + "y" * 20
                for index in range(20)
            )
            history.replace_entries([
                HistoryEntry(source, spacing=0, id="oversized")
            ])
            await history.wait_for_reflow()

            projected, was_projected = history._project_entry_content(source)
            assert was_projected
            assert isinstance(projected, str)
            assert len(projected) <= policy.history_entry_chars
            assert "内容过长" in projected
            assert history.entries[0].content == source
            assert len(history.lines) <= policy.history_window_lines
            assert any("内容过长" in line.text for line in history.lines)

    asyncio.run(scenario())


def test_resize_burst_runs_one_final_window_reflow(monkeypatch) -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_window_entries=50,
            history_resize_debounce=0.1,
            history_reflow_slice=0.001,
        )
        app = _HistoryApp(policy)
        async with app.run_test(size=(100, 24)) as pilot:
            history = app.query_one(HistoryLog)
            history.replace_entries([
                HistoryEntry(
                    f"entry {index}: " + "content " * 8,
                    id=f"entry-{index}",
                )
                for index in range(100)
            ])
            await history.wait_for_reflow()
            await pilot.pause()
            await history.wait_for_reflow()

            render_calls = 0
            render_entry = history._render_entry_lines

            def count_render(entry: HistoryEntry, width: int):
                nonlocal render_calls
                render_calls += 1
                return render_entry(entry, width)

            monkeypatch.setattr(history, "_render_entry_lines", count_render)
            for width in range(70, 95):
                await pilot.resize_terminal(width, 24)
            await history.wait_for_reflow()

            assert render_calls == history.rendered_entry_count
            assert history._render_width == history.scrollable_content_region.width
            assert not history.reflow_pending

    asyncio.run(scenario())


def test_unmount_cancels_resize_and_stream_work() -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(
            history_resize_debounce=1.0,
            stream_short_interval=1.0,
        )
        app = _HistoryApp(policy)
        debounce_task = None
        async with app.run_test(size=(100, 24)) as pilot:
            history = app.query_one(HistoryLog)
            history.append_entry("stable", entry_id="stable")
            await history.wait_for_reflow()
            await pilot.pause()
            await history.wait_for_reflow()

            await pilot.resize_terminal(70, 24)
            debounce_task = history._resize_debounce_task
            assert debounce_task is not None

            history.begin_stream("response", entry_id="response")
            history.append_stream("response", "pending")
            assert history._stream_timer is not None

        assert debounce_task is not None and debounce_task.done()
        assert history._resize_debounce_task is None
        assert history._reflow_task is None
        assert history._stream_timer is None

    asyncio.run(scenario())
