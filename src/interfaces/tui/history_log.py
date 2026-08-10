"""单控件聊天历史、流式尾项和 resize 重排。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Iterable

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.geometry import Offset, Size
from textual.screen import Screen
from textual.selection import SelectEnd, Selection, SelectStart, SelectState
from textual.style import Style as TextualStyle
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import RichLog


_STREAM_RENDER_INTERVAL = 0.05
_REFLOW_BATCH_SIZE = 32


@dataclass(slots=True)
class HistoryEntry:
    """一个可重排的逻辑历史条目。"""

    content: str | Text
    markdown: bool = False
    style: str | None = None
    spacing: int = 1
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def clone(self) -> HistoryEntry:
        content = self.content.copy() if isinstance(self.content, Text) else self.content
        return HistoryEntry(
            content=content,
            markdown=self.markdown,
            style=self.style,
            spacing=self.spacing,
            id=self.id,
        )


@dataclass(slots=True)
class _StreamBuffer:
    entry_id: str
    chunks: list[str] = field(default_factory=list)

    def append(self, content: str) -> None:
        if content:
            self.chunks.append(content)

    def materialize(self) -> str:
        if len(self.chunks) > 1:
            self.chunks[:] = ["".join(self.chunks)]
        return self.chunks[0] if self.chunks else ""


@dataclass(frozen=True, slots=True)
class _ViewportAnchor:
    entry_id: str | None
    line_offset: int
    follow_tail: bool


class HistoryLog(RichLog):
    """保留全量逻辑历史、只以一个 DOM 控件绘制 Rich 行。"""

    FOCUS_ON_CLICK = False

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("max_lines", None)
        kwargs.setdefault("min_width", 1)
        kwargs.setdefault("wrap", True)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("markup", False)
        kwargs.setdefault("auto_scroll", False)
        super().__init__(**kwargs)
        self._entries: list[HistoryEntry] = []
        self._entries_by_id: dict[str, HistoryEntry] = {}
        self._entry_ranges: dict[str, tuple[int, int]] = {}
        self._active_stream_id: str | None = None
        self._streams: dict[str, _StreamBuffer] = {}
        self._stream_timer: Timer | None = None
        self._stream_dirty = False
        self._stream_flush_count = 0
        self._stream_merge_count = 0
        self._source_revision = 0
        self._rendered_revision = 0
        self._render_width = 0
        self._reflow_generation = 0
        self._reflow_task: asyncio.Task[None] | None = None
        self._reflow_anchor: _ViewportAnchor | None = None

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    @property
    def entry_ranges(self) -> dict[str, tuple[int, int]]:
        return dict(self._entry_ranges)

    @property
    def active_stream_id(self) -> str | None:
        return self._active_stream_id

    @property
    def stream_flush_count(self) -> int:
        return self._stream_flush_count

    @property
    def stream_merge_count(self) -> int:
        return self._stream_merge_count

    def on_mount(self) -> None:
        self.anchor()

    async def on_unmount(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        task = self._reflow_task
        self._reflow_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def on_resize(self, event: events.Resize) -> None:
        super().on_resize(event)
        if event.size.width:
            self.call_after_refresh(self._request_current_width_reflow)

    def append_entry(
        self,
        entry: HistoryEntry | str | Text,
        *,
        markdown: bool = False,
        style: str | None = None,
        spacing: int = 1,
        entry_id: str | None = None,
    ) -> str:
        """追加一个完成条目并返回稳定条目 ID。"""
        self._finish_active_stream()
        if not isinstance(entry, HistoryEntry):
            entry = HistoryEntry(
                entry,
                markdown=markdown,
                style=style,
                spacing=spacing,
                id=entry_id or uuid.uuid4().hex,
            )
        else:
            entry = entry.clone()
        self._append_source_entry(entry)
        return entry.id

    def begin_stream(
        self,
        stream_id: str,
        initial: str = "",
        *,
        markdown: bool = True,
        style: str | None = None,
        spacing: int = 1,
        entry_id: str | None = None,
    ) -> str:
        """建立唯一活动尾项；已有流会先完整收尾。"""
        if self._active_stream_id == stream_id:
            return self._streams[stream_id].entry_id
        self._finish_active_stream()
        entry = HistoryEntry(
            initial,
            markdown=markdown,
            style=style,
            spacing=spacing,
            id=entry_id or uuid.uuid4().hex,
        )
        buffer = _StreamBuffer(entry.id)
        buffer.append(initial)
        self._active_stream_id = stream_id
        self._streams[stream_id] = buffer
        self._append_source_entry(entry)
        return entry.id

    def append_stream(self, stream_id: str, content: str) -> None:
        """O(1) 追加 delta，并把下一次尾项渲染合并到 50 ms 窗口。"""
        if not content:
            return
        if self._active_stream_id != stream_id:
            self.begin_stream(stream_id)
        buffer = self._streams[stream_id]
        buffer.append(content)
        self._stream_dirty = True
        if self._stream_timer is not None:
            self._stream_merge_count += 1
            return
        if not self.is_mounted:
            return
        self._stream_timer = self.set_timer(
            _STREAM_RENDER_INTERVAL,
            self._flush_stream,
        )

    def end_stream(self, stream_id: str) -> str:
        """同步提交流式尾项的最终完整文本。"""
        if self._active_stream_id != stream_id:
            return ""
        return self._finish_active_stream()

    def replace_entries(self, entries: Iterable[HistoryEntry]) -> None:
        """批量替换逻辑源，只触发一次分批行重建。"""
        self._discard_stream()
        new_entries = [entry.clone() for entry in entries]
        ids = [entry.id for entry in new_entries]
        if len(ids) != len(set(ids)):
            raise ValueError("HistoryEntry.id must be unique")
        self._entries = new_entries
        self._entries_by_id = {entry.id: entry for entry in new_entries}
        self._source_revision += 1
        if self._size_known and self.is_mounted:
            self._request_current_width_reflow()
        else:
            super().clear()
            self._entry_ranges.clear()
            self._rendered_revision = 0

    def clear(self) -> HistoryLog:
        self._discard_stream()
        self._entries.clear()
        self._entries_by_id.clear()
        self._entry_ranges.clear()
        self._source_revision += 1
        self._rendered_revision = self._source_revision
        super().clear()
        return self

    async def wait_for_reflow(self) -> None:
        """等待当前和被合并进来的 resize 重排完成。"""
        while self._reflow_task is not None:
            task = self._reflow_task
            await asyncio.gather(task, return_exceptions=True)
            if self._reflow_task is task:
                break

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """只读取选区覆盖的 Rich 行，避免拼接完整历史。"""
        if not self.lines:
            return ("", "\n")
        start = selection.start or Offset(0, 0)
        end = selection.end or Offset(len(self.lines[-1].text), len(self.lines) - 1)
        start_y = min(max(0, start.y), len(self.lines) - 1)
        end_y = min(max(0, end.y), len(self.lines) - 1)
        if (end_y, end.x) < (start_y, start.x):
            start, end = end, start
            start_y, end_y = end_y, start_y
        selected_lines = [
            self.lines[index].text.rstrip(" ")
            for index in range(start_y, end_y + 1)
        ]
        if not selected_lines:
            return ("", "\n")
        if start_y == end_y:
            text = selected_lines[0][start.x:end.x]
        else:
            selected_lines[0] = selected_lines[0][start.x:]
            selected_lines[-1] = selected_lines[-1][:end.x]
            text = "\n".join(selected_lines)
        return (text, "\n")

    def render_line(self, y: int) -> Strip:
        """为可见 Rich 行补充精确选取坐标并绘制选区。"""
        scroll_x, scroll_y = self.scroll_offset
        line_y = scroll_y + y
        strip = super().render_line(y).apply_offsets(scroll_x, line_y)
        selection = self.text_selection
        if selection is None or (span := selection.get_span(line_y)) is None:
            return strip

        selection_style = TextualStyle.from_styles(
            self.screen.get_component_styles("screen--selection")
        ).rich_style
        return self._apply_selection_style(strip, line_y, span, selection_style)

    @staticmethod
    def _apply_selection_style(
        strip: Strip,
        line_y: int,
        span: tuple[int, int],
        selection_style: RichStyle,
    ) -> Strip:
        start, end = span
        selected_segments: list[Segment] = []
        for segment in strip:
            text, style, control = segment
            if control is not None or not text or style is None or style._meta is None:
                selected_segments.append(segment)
                continue
            segment_start, _segment_y = style.meta["offset"]
            segment_end = segment_start + len(text)
            overlap_start = max(start, segment_start)
            overlap_end = segment_end if end == -1 else min(end, segment_end)
            if overlap_start >= overlap_end:
                selected_segments.append(segment)
                continue

            left = overlap_start - segment_start
            right = overlap_end - segment_start
            for part, part_start, selected in (
                (text[:left], segment_start, False),
                (text[left:right], overlap_start, True),
                (text[right:], overlap_end, False),
            ):
                if not part:
                    continue
                part_style = style
                if selected:
                    part_style += selection_style
                part_style += RichStyle.from_meta({"offset": (part_start, line_y)})
                selected_segments.append(Segment(part, part_style))
        return Strip(selected_segments, strip.cell_length)

    def on_mouse_down(self, _event: events.MouseDown) -> None:
        state = self.screen._select_state
        if state is None or state.end is not None:
            return
        start = state.start
        if (
            start.container is not self
            and self not in start.container.ancestors
            and start.content_widget is not self
        ):
            return
        self.release_anchor()
        if start.container is self:
            return
        normalized_start = SelectStart(
            container=self,
            container_pointer_delta=state.screen_offset - self.region.offset,
            container_initial_offset=self.region.offset,
            container_initial_scroll_offset=self.scroll_offset,
            content_widget=start.content_widget,
            content_offset=start.content_offset,
        )
        self.screen._select_state = SelectState(state.screen_offset, normalized_start)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < self.max_scroll_y - 1:
            self.anchor(False)
        scroll_delta = round(new_value) - round(old_value)
        if not scroll_delta:
            return
        state = self.screen._select_state
        if (
            not self.screen._selecting
            or state is None
            or state.end is None
            or state.start.container is not self
        ):
            return
        start = state.start
        adjusted_start = start._replace(
            container_initial_scroll_offset=start.container_initial_scroll_offset
            + Offset(0, scroll_delta * 2)
        )
        self.screen.set_reactive(
            Screen._select_state,
            state._replace(start=adjusted_start),
        )
        if not getattr(self.screen, "_selection_auto_scroll_step", False):
            self.screen._update_select()

    def resume_anchor_at_end(self) -> None:
        if self.scroll_y >= self.max_scroll_y - 1:
            self.anchor()

    def _append_source_entry(self, entry: HistoryEntry) -> None:
        if entry.id in self._entries_by_id:
            raise ValueError(f"duplicate HistoryEntry.id: {entry.id}")
        follow_tail = self._is_following_tail()
        self._entries.append(entry)
        self._entries_by_id[entry.id] = entry
        self._source_revision += 1
        if self._size_known and self._render_width and self._reflow_task is None:
            start = len(self.lines)
            rendered = self._render_entry_lines(entry, self._render_width)
            self.lines.extend(rendered)
            self._entry_ranges[entry.id] = (start, len(self.lines))
            self._commit_line_metadata(self._render_width)
            self._rendered_revision = self._source_revision
            if follow_tail:
                self._scroll_to_end()
            return
        if self._size_known and self.is_mounted:
            self._request_current_width_reflow()

    def _finish_active_stream(self) -> str:
        stream_id = self._active_stream_id
        if stream_id is None:
            return ""
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        buffer = self._streams.pop(stream_id)
        self._active_stream_id = None
        content = buffer.materialize()
        entry = self._entries_by_id.get(buffer.entry_id)
        if entry is not None:
            entry.content = content
            self._source_revision += 1
            self._replace_rendered_tail(entry)
        self._stream_dirty = False
        return content

    def _discard_stream(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        self._active_stream_id = None
        self._streams.clear()
        self._stream_dirty = False

    def _flush_stream(self) -> None:
        self._stream_timer = None
        if not self._stream_dirty or self._active_stream_id is None:
            return
        self._stream_dirty = False
        buffer = self._streams[self._active_stream_id]
        entry = self._entries_by_id.get(buffer.entry_id)
        if entry is None:
            return
        entry.content = buffer.materialize()
        self._source_revision += 1
        self._stream_flush_count += 1
        self._replace_rendered_tail(entry)

    def _replace_rendered_tail(self, entry: HistoryEntry) -> None:
        row_range = self._entry_ranges.get(entry.id)
        if (
            row_range is None
            or row_range[1] != len(self.lines)
            or not self._render_width
            or self._reflow_task is not None
        ):
            if self._size_known and self.is_mounted:
                self._request_current_width_reflow()
            return
        follow_tail = self._is_following_tail()
        del self.lines[row_range[0]:]
        self.lines.extend(self._render_entry_lines(entry, self._render_width))
        self._entry_ranges[entry.id] = (row_range[0], len(self.lines))
        self._commit_line_metadata(self._render_width)
        self._rendered_revision = self._source_revision
        if follow_tail:
            self._scroll_to_end()

    def _render_entry_lines(self, entry: HistoryEntry, width: int) -> list[Strip]:
        content = entry.content
        if entry.markdown:
            source = content.plain if isinstance(content, Text) else content
            renderable = RichMarkdown(source, style=entry.style or "none")
        elif isinstance(content, Text):
            renderable = content.copy()
            if entry.style:
                renderable.stylize(entry.style)
            renderable.expand_tabs()
        else:
            renderable = Text(content, style=entry.style)
            renderable.expand_tabs()
        options = self.app.console.options.update_width(width)
        segments = self.app.console.render(renderable, options)
        rich_lines = list(Segment.split_lines(segments))
        if rich_lines:
            strips = Strip.from_lines(rich_lines)
            for strip in strips:
                strip.adjust_cell_length(width)
        else:
            strips = [Strip.blank(width)]
        strips.extend(Strip.blank(width) for _ in range(max(0, entry.spacing)))
        return strips

    def _request_current_width_reflow(self) -> None:
        if not self._size_known or not self.is_mounted:
            return
        width = max(1, self.scrollable_content_region.width)
        if (
            width == self._render_width
            and self._rendered_revision == self._source_revision
            and self._reflow_task is None
        ):
            return
        self._reflow_generation += 1
        self._reflow_anchor = self._capture_viewport_anchor()
        if self._reflow_task is None:
            self._reflow_task = asyncio.create_task(
                self._reflow_worker(),
                name="history-log-reflow",
            )

    async def _reflow_worker(self) -> None:
        current_task = asyncio.current_task()
        try:
            # Textual 在 Python 3.12+ 使用 eager task factory。空历史没有批次可
            # await，若不先让出，协程会在 _reflow_task 赋值前结束并留下失效引用。
            await asyncio.sleep(0)
            while self.is_mounted:
                generation = self._reflow_generation
                revision = self._source_revision
                width = max(1, self.scrollable_content_region.width)
                entries = list(self._entries)
                lines: list[Strip] = []
                ranges: dict[str, tuple[int, int]] = {}
                stale = False
                for batch_start in range(0, len(entries), _REFLOW_BATCH_SIZE):
                    for entry in entries[batch_start:batch_start + _REFLOW_BATCH_SIZE]:
                        start = len(lines)
                        lines.extend(self._render_entry_lines(entry, width))
                        ranges[entry.id] = (start, len(lines))
                    await asyncio.sleep(0)
                    if (
                        generation != self._reflow_generation
                        or revision != self._source_revision
                        or width != max(1, self.scrollable_content_region.width)
                    ):
                        stale = True
                        break
                if stale:
                    continue
                anchor = self._reflow_anchor or _ViewportAnchor(None, 0, True)
                self.lines = lines
                self._entry_ranges = ranges
                self._render_width = width
                self._rendered_revision = revision
                self._commit_line_metadata(width)
                self._restore_viewport_anchor(anchor)
                if generation == self._reflow_generation:
                    break
        finally:
            if self._reflow_task is current_task:
                self._reflow_task = None

    def _commit_line_metadata(self, width: int) -> None:
        self._line_cache.clear()
        self._widest_line_width = width if self.lines else 0
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh()

    def _capture_viewport_anchor(self) -> _ViewportAnchor:
        follow_tail = self._is_following_tail()
        if follow_tail or not self._entry_ranges:
            return _ViewportAnchor(None, 0, follow_tail)
        line = min(max(0, round(self.scroll_y)), max(0, len(self.lines) - 1))
        for entry in self._entries:
            row_range = self._entry_ranges.get(entry.id)
            if row_range is not None and row_range[0] <= line < row_range[1]:
                return _ViewportAnchor(entry.id, line - row_range[0], False)
        return _ViewportAnchor(None, line, False)

    def _restore_viewport_anchor(self, anchor: _ViewportAnchor) -> None:
        if anchor.follow_tail:
            self._scroll_to_end()
            return
        row_range = self._entry_ranges.get(anchor.entry_id or "")
        target = (
            row_range[0] + min(anchor.line_offset, max(0, row_range[1] - row_range[0] - 1))
            if row_range is not None
            else anchor.line_offset
        )
        self.scroll_to(y=target, animate=False, immediate=True)

    def _is_following_tail(self) -> bool:
        return not self.lines or self.scroll_y >= self.max_scroll_y - 1

    def _scroll_to_end(self) -> None:
        self.scroll_end(animate=False, immediate=True, x_axis=False)
        self.anchor()
