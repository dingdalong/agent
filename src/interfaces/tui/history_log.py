"""单控件聊天历史、流式尾项和 resize 重排。"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from io import StringIO
from typing import Iterable, Literal

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

from src.interfaces.tui.diagnostics import TuiDiagnostics
from src.interfaces.tui.render_policy import TuiRenderPolicy
from src.interfaces.tui.widgets import PointerScrollMixin


_OMISSION_NOTICE = "\n\n[内容过长，中间部分未渲染]\n\n"


@dataclass(slots=True)
class HistoryEntry:
    """一个可重排的逻辑历史条目。"""

    content: str | Text
    markdown: bool = False
    style: str | None = None
    spacing: int = 1
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    revision: int = 0

    def clone(self) -> HistoryEntry:
        content = self.content.copy() if isinstance(self.content, Text) else self.content
        return HistoryEntry(
            content=content,
            markdown=self.markdown,
            style=self.style,
            spacing=self.spacing,
            id=self.id,
            revision=self.revision,
        )


@dataclass(slots=True)
class _StreamBuffer:
    entry_id: str
    buffer: StringIO = field(default_factory=StringIO)
    length: int = 0

    def append(self, content: str) -> None:
        if content:
            self.buffer.write(content)
            self.length += len(content)

    def materialize(self) -> str:
        return self.buffer.getvalue()


@dataclass(frozen=True, slots=True)
class _ViewportAnchor:
    entry_id: str | None
    line_offset: int
    follow_tail: bool


def _first_strip_difference(left: list[Strip], right: list[Strip]) -> int | None:
    common_length = min(len(left), len(right))
    for index in range(common_length):
        if left[index] != right[index]:
            return index
    return common_length if len(left) != len(right) else None


class HistoryLog(PointerScrollMixin, RichLog):
    """保留轻量逻辑历史，只渲染有预算的分页窗口。"""

    FOCUS_ON_CLICK = False

    def __init__(
        self,
        *,
        policy: TuiRenderPolicy | None = None,
        diagnostics: TuiDiagnostics | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("max_lines", None)
        kwargs.setdefault("min_width", 1)
        kwargs.setdefault("wrap", True)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("markup", False)
        kwargs.setdefault("auto_scroll", False)
        super().__init__(**kwargs)
        self._policy = policy or TuiRenderPolicy()
        self._diagnostics = diagnostics
        self._entries: list[HistoryEntry] = []
        self._entries_by_id: dict[str, HistoryEntry] = {}
        self._entry_ranges: dict[str, tuple[int, int]] = {}
        self._window_start = 0
        self._window_end = 0
        self._rendered_window = (0, 0)
        self._window_bias: Literal["start", "end"] = "end"
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
        self._resize_debounce_task: asyncio.Task[None] | None = None
        self._reflow_anchor: _ViewportAnchor | None = None
        self._resize_anchor: _ViewportAnchor | None = None
        self._reflow_reason = "initial"
        self._cancelled_reflows = 0
        self._page_shift_pending = False
        self._renderable_cache: dict[str, tuple[int, object]] = {}

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    @property
    def entry_ranges(self) -> dict[str, tuple[int, int]]:
        return dict(self._entry_ranges)

    @property
    def window_range(self) -> tuple[int, int]:
        return self._window_start, self._window_end

    @property
    def rendered_entry_count(self) -> int:
        return self._window_end - self._window_start

    @property
    def has_older(self) -> bool:
        return self._window_start > 0

    @property
    def has_newer(self) -> bool:
        return self._window_end < len(self._entries)

    @property
    def reflow_pending(self) -> bool:
        return self._resize_debounce_task is not None or self._reflow_task is not None

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
        debounce_task = self._resize_debounce_task
        self._resize_debounce_task = None
        if debounce_task is not None and not debounce_task.done():
            debounce_task.cancel()
        task = self._reflow_task
        self._reflow_task = None
        if task is not None and not task.done():
            task.cancel()
        await asyncio.gather(
            *(item for item in (debounce_task, task) if item is not None),
            return_exceptions=True,
        )

    def on_resize(self, event: events.Resize) -> None:
        super().on_resize(event)
        if event.size.width:
            self.call_after_refresh(self._schedule_resize_reflow)

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
        """O(1) 追加 delta，并按累计长度自适应合并尾项渲染。"""
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
            self._policy.stream_interval(buffer.length),
            self._flush_stream,
        )

    def end_stream(self, stream_id: str) -> str:
        """同步提交流式尾项的最终完整文本。"""
        if self._active_stream_id != stream_id:
            return ""
        return self._finish_active_stream()

    def replace_entries(self, entries: Iterable[HistoryEntry]) -> None:
        """批量替换逻辑源，初始只渲染尾部窗口。"""
        self._discard_stream()
        new_entries = [entry.clone() for entry in entries]
        ids = [entry.id for entry in new_entries]
        if len(ids) != len(set(ids)):
            raise ValueError("HistoryEntry.id must be unique")
        self._entries = new_entries
        self._entries_by_id = {entry.id: entry for entry in new_entries}
        self._window_end = len(new_entries)
        self._window_start = self._fit_window_backward(self._window_end)
        self._window_bias = "end"
        self._renderable_cache.clear()
        self._source_revision += 1
        tail_anchor = _ViewportAnchor(None, 0, True)
        self._reflow_anchor = tail_anchor
        if self._resize_debounce_task is not None:
            self._resize_anchor = tail_anchor
        if self._size_known and self.is_mounted:
            self._request_current_width_reflow(
                reason="hydrate",
                capture_anchor=False,
            )
        else:
            super().clear()
            self._entry_ranges.clear()
            self._rendered_window = (0, 0)
            self._rendered_revision = 0

    def clear(self) -> HistoryLog:
        self._discard_stream()
        self._entries.clear()
        self._entries_by_id.clear()
        self._entry_ranges.clear()
        self._window_start = 0
        self._window_end = 0
        self._rendered_window = (0, 0)
        self._renderable_cache.clear()
        self._source_revision += 1
        self._rendered_revision = self._source_revision
        super().clear()
        return self

    async def wait_for_reflow(self) -> None:
        """等待 resize 去抖和被合并进来的重排全部完成。"""
        while True:
            tasks = [
                task
                for task in (self._resize_debounce_task, self._reflow_task)
                if task is not None
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

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
        if new_value < self.max_scroll_y - 1 or self.has_newer:
            self.anchor(False)
        scroll_delta = round(new_value) - round(old_value)
        if not scroll_delta:
            return
        state = self.screen._select_state
        selecting_history = bool(
            self.screen._selecting
            and state is not None
            and state.start.container is self
        )
        if not selecting_history and not self.reflow_pending and not self._page_shift_pending:
            if scroll_delta < 0 and new_value <= 1 and self.has_older:
                self._page_shift_pending = True
                self.call_after_refresh(lambda: self._shift_page("older"))
            elif (
                scroll_delta > 0
                and new_value >= self.max_scroll_y - 1
                and self.has_newer
            ):
                self._page_shift_pending = True
                self.call_after_refresh(lambda: self._shift_page("newer"))
        if (
            not selecting_history
            or state is None
            or state.end is None
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
        if not self.has_newer and self.scroll_y >= self.max_scroll_y - 1:
            self.anchor()

    def jump_to_tail(self) -> None:
        """切回最新分页窗口并跟随尾部。"""
        self._window_end = len(self._entries)
        self._window_start = self._fit_window_backward(self._window_end)
        self._window_bias = "end"
        tail_anchor = _ViewportAnchor(None, 0, True)
        self._reflow_anchor = tail_anchor
        if self._resize_debounce_task is not None:
            self._resize_anchor = tail_anchor
        self._request_current_width_reflow(reason="jump_tail", capture_anchor=False)

    def _shift_page(self, direction: Literal["older", "newer"]) -> None:
        self._page_shift_pending = False
        if self.reflow_pending:
            return
        anchor = self._capture_viewport_anchor()
        page_size = max(1, self._policy.history_page_entries)
        if direction == "older":
            if not self.has_older:
                return
            self._window_start = max(0, self._window_start - page_size)
            self._window_end = self._fit_window_forward(self._window_start)
            self._window_bias = "start"
        else:
            if not self.has_newer:
                return
            self._window_end = min(len(self._entries), self._window_end + page_size)
            self._window_start = self._fit_window_backward(self._window_end)
            self._window_bias = "end"
        self._reflow_anchor = anchor
        self._request_current_width_reflow(
            reason=f"page_{direction}",
            capture_anchor=False,
        )

    def _fit_window_backward(self, end: int) -> int:
        limit_entries = max(1, self._policy.history_window_entries)
        limit_chars = max(1, self._policy.history_window_chars)
        start = end
        chars = 0
        while start > 0 and end - start < limit_entries:
            length = self._entry_length(self._entries[start - 1])
            if start < end and chars + length > limit_chars:
                break
            start -= 1
            chars += length
        return start

    def _fit_window_forward(self, start: int) -> int:
        limit_entries = max(1, self._policy.history_window_entries)
        limit_chars = max(1, self._policy.history_window_chars)
        end = start
        chars = 0
        while end < len(self._entries) and end - start < limit_entries:
            length = self._entry_length(self._entries[end])
            if end > start and chars + length > limit_chars:
                break
            chars += length
            end += 1
        return end

    @staticmethod
    def _entry_length(entry: HistoryEntry) -> int:
        content = entry.content
        return len(content.plain if isinstance(content, Text) else content)

    def _refresh_newer_sentinel(self) -> None:
        if not self.has_newer or not self._render_width:
            return
        sentinel = self._render_sentinel("newer", self._render_width)
        rendered_end = max((row_range[1] for row_range in self._entry_ranges.values()), default=0)
        if rendered_end == len(self.lines):
            line = len(self.lines)
            self.lines.append(sentinel)
        else:
            line = len(self.lines) - 1
            self.lines[line] = sentinel
        self._rendered_revision = self._source_revision
        self._commit_line_metadata(
            self._render_width,
            changed_range=(line, line + 1),
        )

    def _append_source_entry(self, entry: HistoryEntry) -> None:
        if entry.id in self._entries_by_id:
            raise ValueError(f"duplicate HistoryEntry.id: {entry.id}")
        follow_tail = self._is_following_tail()
        source_was_at_tail = self._window_end == len(self._entries)
        old_window_start = self._window_start
        old_window_end = self._window_end
        self._entries.append(entry)
        self._entries_by_id[entry.id] = entry
        self._source_revision += 1
        if not source_was_at_tail or not follow_tail:
            self._refresh_newer_sentinel()
            return
        self._window_end = len(self._entries)
        self._window_start = self._fit_window_backward(self._window_end)
        self._window_bias = "end"
        can_append = (
            self._window_start == old_window_start
            and self._rendered_window == (old_window_start, old_window_end)
            and self._size_known
            and self._render_width
            and not self.reflow_pending
            and self._rendered_revision == self._source_revision - 1
        )
        if can_append:
            start = len(self.lines)
            rendered = self._render_entry_lines(entry, self._render_width)
            line_limit = max(1, self._policy.history_window_lines - 1)
            if len(self.lines) + len(rendered) > line_limit:
                self._request_current_width_reflow(reason="append_trim")
                return
            self.lines.extend(rendered)
            self._entry_ranges[entry.id] = (start, len(self.lines))
            self._rendered_window = (self._window_start, self._window_end)
            self._commit_line_metadata(
                self._render_width,
                changed_range=(start, len(self.lines)),
            )
            self._rendered_revision = self._source_revision
            if follow_tail:
                self._scroll_to_end()
            return
        if self._size_known and self.is_mounted:
            self._request_current_width_reflow(reason="append")

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
        if entry is not None and entry.content != content:
            entry.content = content
            entry.revision += 1
            self._renderable_cache.pop(entry.id, None)
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
        buffer = self._streams[self._active_stream_id]
        entry = self._entries_by_id.get(buffer.entry_id)
        if entry is None:
            return
        if self.reflow_pending:
            self._stream_timer = self.set_timer(
                self._policy.stream_interval(buffer.length),
                self._flush_stream,
            )
            return
        if entry.id not in self._entry_ranges:
            return
        self._stream_dirty = False
        entry.content = buffer.materialize()
        entry.revision += 1
        self._renderable_cache.pop(entry.id, None)
        self._source_revision += 1
        self._stream_flush_count += 1
        self._replace_rendered_tail(entry)

    def _replace_rendered_tail(self, entry: HistoryEntry) -> None:
        row_range = self._entry_ranges.get(entry.id)
        if (
            row_range is None
            or row_range[1] != len(self.lines)
            or not self._render_width
        ):
            if row_range is not None and self._size_known and self.is_mounted:
                self._request_current_width_reflow(reason="stream")
            return
        follow_tail = self._is_following_tail()
        start = row_range[0]
        old_tail = self.lines[start:]
        new_tail = self._render_entry_lines(entry, self._render_width)
        line_budget = max(1, self._policy.history_window_lines - start - 1)
        needs_window_trim = len(new_tail) > line_budget
        needs_window_reflow = needs_window_trim and self.rendered_entry_count > 1
        if needs_window_trim:
            new_tail = self._limit_entry_lines(
                new_tail,
                self._render_width,
                line_budget,
            )
        first_difference = _first_strip_difference(old_tail, new_tail)
        if first_difference is None:
            self._rendered_revision = self._source_revision
            if follow_tail:
                self._scroll_to_end()
            return
        changed_start = start + first_difference
        changed_end = start + max(len(old_tail), len(new_tail))
        self.lines[changed_start:] = new_tail[first_difference:]
        self._entry_ranges[entry.id] = (row_range[0], len(self.lines))
        self._commit_line_metadata(
            self._render_width,
            changed_range=(changed_start, changed_end),
        )
        self._rendered_revision = self._source_revision
        if follow_tail:
            self._scroll_to_end()
        if needs_window_reflow:
            self._rendered_revision = max(0, self._source_revision - 1)
            self._request_current_width_reflow(reason="stream_trim")

    def _render_entry_lines(self, entry: HistoryEntry, width: int) -> list[Strip]:
        renderable = self._entry_renderable(entry)
        return self._render_renderable_lines(renderable, width, entry.spacing)

    def _entry_renderable(self, entry: HistoryEntry) -> object:
        cached = self._renderable_cache.get(entry.id)
        if cached is not None and cached[0] == entry.revision:
            return cached[1]
        content, projected = self._project_entry_content(entry.content)
        if entry.markdown and not projected:
            source = content.plain if isinstance(content, Text) else content
            renderable: object = RichMarkdown(source, style=entry.style or "none")
        elif isinstance(content, Text):
            rendered_text = content.copy()
            if entry.style:
                rendered_text.stylize(entry.style)
            rendered_text.expand_tabs()
            renderable = rendered_text
        else:
            rendered_text = Text(content, style=entry.style)
            rendered_text.expand_tabs()
            renderable = rendered_text
        self._renderable_cache[entry.id] = (entry.revision, renderable)
        return renderable

    def _project_entry_content(self, content: str | Text) -> tuple[str | Text, bool]:
        plain = content.plain if isinstance(content, Text) else content
        char_limit = max(1, self._policy.history_entry_chars)
        line_limit = max(2, self._policy.history_entry_source_lines)
        if len(plain) <= char_limit and plain.count("\n") < line_limit:
            return content, False
        if char_limit <= len(_OMISSION_NOTICE):
            return plain[:char_limit], True
        content_chars = char_limit - len(_OMISSION_NOTICE)
        head_chars = content_chars // 2
        tail_chars = content_chars - head_chars
        half_lines = max(1, line_limit // 2)
        head = "".join(plain[:head_chars].splitlines(keepends=True)[:half_lines])
        tail = "".join(plain[-tail_chars:].splitlines(keepends=True)[-half_lines:])
        return head + _OMISSION_NOTICE + tail, True

    def _render_renderable_lines(
        self,
        renderable: object,
        width: int,
        spacing: int = 0,
    ) -> list[Strip]:
        options = self.app.console.options.update_width(width)
        segments = self.app.console.render(renderable, options)
        rich_lines = list(Segment.split_lines(segments))
        if rich_lines:
            strips = Strip.from_lines(rich_lines)
            for strip in strips:
                strip.adjust_cell_length(width)
        else:
            strips = [Strip.blank(width)]
        strips.extend(Strip.blank(width) for _ in range(max(0, spacing)))
        return strips

    def _render_sentinel(
        self,
        direction: Literal["older", "newer"],
        width: int,
    ) -> Strip:
        if direction == "older":
            count = self._window_start
            label = f"↑ 较早历史 {count} 条"
        else:
            count = len(self._entries) - self._window_end
            label = f"↓ 较新历史 {count} 条"
        return self._render_renderable_lines(
            Text(label, style="bright_black"),
            width,
        )[0]

    def _render_omission_line(self, width: int) -> Strip:
        return self._render_renderable_lines(
            Text("[单条内容过长，部分渲染行已省略]", style="yellow"),
            width,
        )[0]

    def _limit_entry_lines(
        self,
        lines: list[Strip],
        width: int,
        limit: int,
    ) -> list[Strip]:
        limit = max(1, limit)
        if len(lines) <= limit:
            return lines
        if limit == 1:
            return [self._render_omission_line(width)]
        head = (limit - 1) // 2
        tail = limit - 1 - head
        return lines[:head] + [self._render_omission_line(width)] + lines[-tail:]

    def _schedule_resize_reflow(self) -> None:
        if not self._size_known or not self.is_mounted:
            return
        if not self._render_width:
            self._request_current_width_reflow(reason="initial")
            return
        width = max(1, self.scrollable_content_region.width)
        if (
            width == self._render_width
            and self._rendered_revision == self._source_revision
            and self._rendered_window == (self._window_start, self._window_end)
        ):
            return
        if self._resize_debounce_task is None:
            self._resize_anchor = self._capture_viewport_anchor()
        self._reflow_generation += 1
        task = self._reflow_task
        if task is not None and not task.done():
            task.cancel()
            self._cancelled_reflows += 1
        debounce = self._resize_debounce_task
        if debounce is not None and not debounce.done():
            debounce.cancel()
        self._resize_debounce_task = asyncio.create_task(
            self._debounced_resize_reflow(),
            name="history-log-resize-debounce",
        )

    async def _debounced_resize_reflow(self) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(max(0, self._policy.history_resize_debounce))
        except asyncio.CancelledError:
            return
        finally:
            if self._resize_debounce_task is current_task:
                self._resize_debounce_task = None
        if not self.is_mounted:
            return
        self._reflow_anchor = self._resize_anchor
        self._resize_anchor = None
        self._request_current_width_reflow(reason="resize", capture_anchor=False)

    def _request_current_width_reflow(
        self,
        *,
        reason: str = "content",
        capture_anchor: bool = True,
    ) -> None:
        if not self._size_known or not self.is_mounted:
            return
        if self._resize_debounce_task is not None and reason != "resize":
            self._reflow_generation += 1
            self._reflow_reason = reason
            return
        width = max(1, self.scrollable_content_region.width)
        if (
            width == self._render_width
            and self._rendered_revision == self._source_revision
            and self._rendered_window == (self._window_start, self._window_end)
            and self._reflow_task is None
        ):
            return
        self._reflow_generation += 1
        self._reflow_reason = reason
        if capture_anchor:
            self._reflow_anchor = self._capture_viewport_anchor()
        task = self._reflow_task
        if task is None or task.done() or task.cancelling():
            self._reflow_task = asyncio.create_task(
                self._reflow_worker(),
                name="history-log-reflow",
            )

    async def _reflow_worker(self) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(0)
            while self.is_mounted:
                if self._resize_debounce_task is not None:
                    return
                self._materialize_visible_stream()
                generation = self._reflow_generation
                revision = self._source_revision
                width = max(1, self.scrollable_content_region.width)
                window_start = self._window_start
                window_end = self._window_end
                entries = list(self._entries[window_start:window_end])
                rendered: list[tuple[HistoryEntry, list[Strip]]] = []
                stale = False
                started = time.perf_counter()
                slice_started = started
                for entry in entries:
                    rendered.append((entry, self._render_entry_lines(entry, width)))
                    if time.perf_counter() - slice_started < self._policy.history_reflow_slice:
                        continue
                    await asyncio.sleep(0)
                    slice_started = time.perf_counter()
                    stale = self._reflow_is_stale(
                        generation,
                        revision,
                        width,
                        window_start,
                        window_end,
                    )
                    if stale:
                        break
                if stale:
                    if self._resize_debounce_task is not None:
                        return
                    continue
                window_start, window_end, rendered = self._trim_rendered_window(
                    window_start,
                    window_end,
                    rendered,
                    width,
                )
                self._window_start = window_start
                self._window_end = window_end
                lines: list[Strip] = []
                ranges: dict[str, tuple[int, int]] = {}
                if self.has_older:
                    lines.append(self._render_sentinel("older", width))
                for entry, entry_lines in rendered:
                    start = len(lines)
                    lines.extend(entry_lines)
                    ranges[entry.id] = (start, len(lines))
                if self.has_newer:
                    lines.append(self._render_sentinel("newer", width))
                anchor = self._reflow_anchor or _ViewportAnchor(None, 0, True)
                self.lines = lines
                self._entry_ranges = ranges
                self._render_width = width
                self._rendered_revision = revision
                self._rendered_window = (window_start, window_end)
                self._commit_line_metadata(width)
                self._restore_viewport_anchor(anchor)
                self._reflow_anchor = None
                self._prune_renderable_cache()
                self._record_reflow(
                    reason=self._reflow_reason,
                    width=width,
                    started=started,
                    rendered=rendered,
                )
                if generation == self._reflow_generation:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            if self._reflow_task is current_task:
                self._reflow_task = None

    def _materialize_visible_stream(self) -> None:
        if not self._stream_dirty or self._active_stream_id is None:
            return
        buffer = self._streams.get(self._active_stream_id)
        if buffer is None or buffer.entry_id not in {
            entry.id for entry in self._entries[self._window_start:self._window_end]
        }:
            return
        entry = self._entries_by_id.get(buffer.entry_id)
        if entry is None:
            return
        entry.content = buffer.materialize()
        entry.revision += 1
        self._renderable_cache.pop(entry.id, None)
        self._stream_dirty = False
        self._source_revision += 1

    def _reflow_is_stale(
        self,
        generation: int,
        revision: int,
        width: int,
        window_start: int,
        window_end: int,
    ) -> bool:
        return bool(
            generation != self._reflow_generation
            or revision != self._source_revision
            or width != max(1, self.scrollable_content_region.width)
            or window_start != self._window_start
            or window_end != self._window_end
        )

    def _trim_rendered_window(
        self,
        window_start: int,
        window_end: int,
        rendered: list[tuple[HistoryEntry, list[Strip]]],
        width: int,
    ) -> tuple[int, int, list[tuple[HistoryEntry, list[Strip]]]]:
        line_limit = max(1, self._policy.history_window_lines)
        sentinel_lines = int(window_start > 0) + int(bool(rendered))
        entry_line_limit = max(1, line_limit - sentinel_lines)
        total_lines = sum(len(lines) for _entry, lines in rendered)
        while total_lines > entry_line_limit and len(rendered) > 1:
            if self._window_bias == "start":
                _entry, removed = rendered.pop()
                window_end -= 1
            else:
                _entry, removed = rendered.pop(0)
                window_start += 1
            total_lines -= len(removed)
            sentinel_lines = int(window_start > 0) + int(bool(rendered))
            entry_line_limit = max(1, line_limit - sentinel_lines)
        if rendered and len(rendered[0][1]) > entry_line_limit:
            entry, entry_lines = rendered[0]
            rendered[0] = (
                entry,
                self._limit_entry_lines(entry_lines, width, entry_line_limit),
            )
        return window_start, window_end, rendered

    def _prune_renderable_cache(self) -> None:
        visible_ids = {
            entry.id for entry in self._entries[self._window_start:self._window_end]
        }
        self._renderable_cache = {
            entry_id: cached
            for entry_id, cached in self._renderable_cache.items()
            if entry_id in visible_ids
        }

    def _record_reflow(
        self,
        *,
        reason: str,
        width: int,
        started: float,
        rendered: list[tuple[HistoryEntry, list[Strip]]],
    ) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.record(
            "history_reflow_finished",
            generation=self._reflow_generation,
            reason=reason,
            width=width,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            cancelled_reflows=self._cancelled_reflows,
            source_entries=len(self._entries),
            rendered_entries=len(rendered),
            rendered_chars=sum(self._entry_length(entry) for entry, _lines in rendered),
            rendered_lines=len(self.lines),
            window_start=self._window_start,
            window_end=self._window_end,
        )
        self._cancelled_reflows = 0

    def _commit_line_metadata(
        self,
        width: int,
        *,
        changed_range: tuple[int, int] | None = None,
    ) -> None:
        self._line_cache.clear()
        self._widest_line_width = width if self.lines else 0
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        if changed_range is None:
            self.refresh()
            return
        changed_start, changed_end = changed_range
        visible_start = self.scroll_offset.y
        visible_end = visible_start + self.scrollable_content_region.height
        refresh_start = max(changed_start, visible_start)
        refresh_end = min(changed_end, visible_end)
        if refresh_start < refresh_end:
            self.refresh_lines(refresh_start, refresh_end - refresh_start)

    def _capture_viewport_anchor(self) -> _ViewportAnchor:
        follow_tail = self._is_following_tail()
        if follow_tail or not self._entry_ranges:
            return _ViewportAnchor(None, 0, follow_tail)
        line = min(max(0, round(self.scroll_y)), max(0, len(self.lines) - 1))
        for entry_id, row_range in self._entry_ranges.items():
            if row_range[0] <= line < row_range[1]:
                return _ViewportAnchor(entry_id, line - row_range[0], False)
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
        return bool(
            not self.lines
            or (
                not self.has_newer
                and self._window_end == len(self._entries)
                and self.scroll_y >= self.max_scroll_y - 1
            )
        )

    def _scroll_to_end(self) -> None:
        self.scroll_end(animate=False, immediate=True, x_axis=False)
        self.anchor()
