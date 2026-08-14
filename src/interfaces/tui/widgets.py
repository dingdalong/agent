"""Textual TUI 的基础输入、滚动和列表组件。"""

from __future__ import annotations

import asyncio
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.content import Content
from textual.geometry import Offset
from textual.message import Message
from textual.screen import Screen
from textual.selection import SelectEnd, Selection
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import ListItem, ListView, OptionList, Static, TextArea

from src.mgr.frozen import clean_env


_SELECT_AUTO_SCROLL_FPS = 20
_POINTER_SCROLL_BURST_WINDOW = 0.120


@dataclass(slots=True)
class _PointerScrollBurst:
    direction: Literal[-1, 1] | None = None
    event_times: deque[float] = field(default_factory=deque)

    def reset(self) -> None:
        self.direction = None
        self.event_times.clear()

    def record(self, direction: Literal[-1, 1], event_time: float) -> int:
        if (
            self.direction != direction
            or (
                self.event_times
                and (
                    event_time < self.event_times[-1]
                    or event_time - self.event_times[-1]
                    > _POINTER_SCROLL_BURST_WINDOW
                )
            )
        ):
            self.reset()
        self.direction = direction
        cutoff = event_time - _POINTER_SCROLL_BURST_WINDOW
        while self.event_times and self.event_times[0] < cutoff:
            self.event_times.popleft()
        self.event_times.append(event_time)
        count = len(self.event_times)
        if count <= 2:
            return 1
        if count <= 5:
            return 2
        return 3


class PointerScrollMixin:
    """按同向滚轮事件密度加速垂直滚动。"""

    def __init__(self, *args, **kwargs) -> None:
        self._pointer_scroll_burst = _PointerScrollBurst()
        super().__init__(*args, **kwargs)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._scroll_for_pointer_burst(event, -1)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._scroll_for_pointer_burst(event, 1)

    def _scroll_for_pointer_burst(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
        direction: Literal[-1, 1],
    ) -> None:
        if event.ctrl or event.shift:
            self._pointer_scroll_burst.reset()
            return
        if not self.allow_vertical_scroll:
            self._pointer_scroll_burst.reset()
            return
        multiplier = self._pointer_scroll_burst.record(direction, event.time)
        scrolled = self._scroll_to(
            y=(
                self.scroll_target_y
                + direction * self.app.scroll_sensitivity_y * multiplier
            ),
            animate=False,
            release_anchor=direction < 0,
        )
        event.prevent_default()
        reached_boundary = (direction < 0 and self.scroll_y <= 0) or (
            direction > 0 and self.scroll_y >= self.max_scroll_y
        )
        if not scrolled or reached_boundary:
            self._pointer_scroll_burst.reset()
        if scrolled:
            event.stop()


class SelectionScreen(Screen[None]):
    """限制选区自动滚动频率，并在选区结束或到达边界时停止。"""

    _selection_auto_scroll_step = False
    _selection_update_pending = False

    def _start_auto_scroll(
        self,
        widget: Widget,
        direction: Literal[+1, -1],
        speed: float = 1.0,
    ) -> None:
        assert speed > 0, "Speed should be positive and non-zero"

        def auto_scroll_y(widget: Widget, direction: float) -> None:
            if not self._selecting or self._select_state is None:
                self._stop_auto_scroll()
                return
            old_scroll_y = widget.scroll_y
            self._selection_auto_scroll_step = True
            try:
                widget.scroll_y += direction
                widget.scroll_target_y = widget.scroll_y
            finally:
                self._selection_auto_scroll_step = False
            if widget.scroll_y == old_scroll_y:
                self._stop_auto_scroll()
                return
            if not self._selection_update_pending:
                self._selection_update_pending = True
                self.call_after_refresh(self._update_selection_after_scroll)

        self._stop_auto_scroll()
        lines_to_scroll = (
            direction
            * (self.app.SELECT_AUTO_SCROLL_SPEED / _SELECT_AUTO_SCROLL_FPS)
            * speed
        )
        scroll_callback = partial(auto_scroll_y, widget, lines_to_scroll)
        scroll_callback()
        if self._selecting and self._select_state is not None:
            if direction < 0 and widget.scroll_y <= 0:
                return
            if direction > 0 and widget.scroll_y >= widget.max_scroll_y:
                return
            self._auto_select_scroll_timer = self.set_interval(
                1 / _SELECT_AUTO_SCROLL_FPS,
                scroll_callback,
            )

    def _update_selection_after_scroll(self) -> None:
        try:
            state = self._select_state
            if not self._selecting or state is None:
                return
            select_widget, select_offset = self.get_widget_and_offset_at(
                state.screen_offset.x,
                state.screen_offset.y,
            )
            if select_widget is not None:
                if select_offset is not None:
                    content_widget = select_widget
                    content_offset = select_offset
                    container = (
                        content_widget
                        if isinstance(content_widget, Screen)
                        else content_widget.parent
                    )
                else:
                    content_widget = None
                    content_offset = None
                    container = select_widget
                self.set_reactive(
                    Screen._select_state,
                    state.update_end(
                        state.screen_offset,
                        SelectEnd(container, content_widget, content_offset),
                    ),
                )
            self._update_select()
        finally:
            self._selection_update_pending = False

    def _forward_event(self, event: events.Event) -> None:
        if isinstance(event, events.MouseDown) and not self.app.mouse_captured:
            select_widget, _select_offset = self.get_widget_and_offset_at(
                event.x,
                event.y,
            )
            # Markdown.update() 后，布局缓存可能短暂命中已卸载的段落。
            if select_widget is not None and not select_widget.is_attached:
                self.refresh(layout=True)
                return
        super()._forward_event(event)
        if isinstance(event, events.MouseUp):
            self._stop_auto_scroll()

    def clear_selection(self) -> None:
        self._stop_auto_scroll()
        super().clear_selection()


def _clamp_selection(selection: Selection, lines: list[str]) -> Selection:
    """把越过末行索引的选区偏移收敛到末行行尾。"""
    last_line = Offset(len(lines[-1]), len(lines) - 1)
    start, end = selection
    if start is not None and start.y > last_line.y:
        start = last_line
    if end is not None and end.y > last_line.y:
        end = last_line
    return Selection(start, end)


class SelectionStatic(Static):
    """内容以换行结尾时多渲染一行空行，取词前把越界偏移收敛回末行。

    Textual 8.2.8 用 `Content.split(allow_blank=True)` 渲染，尾随换行会多出一行
    空行并带有真实偏移元数据；而 `Selection.extract` 按 `splitlines()` 取行且不做
    边界检查，落在这行空行上取词会 IndexError 打崩整个 app。
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        # Static.render() 直接返回 self.visual，与基类 self._render() 是同一对象，
        # 不产生额外渲染。
        visual = self.visual
        if not isinstance(visual, (Content, Text)):
            return None
        text = str(visual)
        lines = text.splitlines()
        if lines:
            selection = _clamp_selection(selection, lines)
        return selection.extract(text), "\n"


class KeyboardTextArea(TextArea):
    """只接受键盘定位和编辑的文本输入框。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_blink = False

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        event.prevent_default()
        event.stop()

class AutoGrowTextArea(KeyboardTextArea):
    """键盘优先、随内容自动增高的多行输入框：1 行起，最多 MAX_LINES 行，超出内部滚动。"""

    MAX_LINES = 4

    def on_mount(self) -> None:
        self._autosize()

    def on_resize(self, _event: events.Resize) -> None:
        # 宽度变化会改变软折行数，需要重新计算高度。
        self._autosize()

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._autosize()

    def _autosize(self) -> None:
        if not self.is_mounted:
            return
        # 用折行后的视觉行数（含显式换行与软折行），与 Composer 增高同一口径。
        height = min(self.MAX_LINES, max(1, self.wrapped_document.height))
        current = self.styles.height
        if current is None or int(current.value) != height:
            self.styles.height = height

class FormBodyScroll(VerticalScroll):
    """表单正文滚动容器：承接长表单滚动，但不因鼠标点击抢焦点。"""

    FOCUS_ON_CLICK = False

class KeyboardOptionList(OptionList):
    """只接受键盘选择的选项列表。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    async def _on_click(self, event: events.Click) -> None:
        event.prevent_default()
        event.stop()


class KeyboardNavigation(SelectionStatic, can_focus=True):
    """承接复杂窗口键盘导航焦点的只读区域。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False


class KeyboardListItem(ListItem):
    """禁止鼠标激活的列表项。"""

    ALLOW_SELECT = False

    def _on_click(self, event: events.Click) -> None:
        event.prevent_default()
        event.stop()


class Composer(KeyboardTextArea):
    """主输入框：Enter 提交，Shift+Enter/Ctrl+J 换行。支持鼠标拖选复制。"""

    ALLOW_SELECT = True

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("tab", "tab_or_complete", show=False, priority=True),
        Binding("escape", "escape_or_clear", show=False, priority=True),
    ]

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        # 恢复 TextArea 原生点击定位/拖选（不再像 KeyboardTextArea 那样拦截）。
        await super(KeyboardTextArea, self)._on_mouse_down(event)

    class Submitted(Message):
        """用户请求提交当前输入。"""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        if getattr(self.app, "apply_completion", lambda: False)():
            return
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        if not self.read_only:
            self.insert("\n")

    def action_tab_or_complete(self) -> None:
        if not getattr(self.app, "select_next_completion", lambda: False)():
            self.app.action_focus_next()

    def action_escape_or_clear(self) -> None:
        getattr(self.app, "hide_completion", lambda: None)()

    def action_cursor_down(self, select: bool = False) -> None:
        if getattr(self.app, "completion_visible", False):
            self.app.select_next_completion()
            return
        if getattr(self.app, "history_next", lambda: False)():
            return
        lines = self.text.split("\n")
        row, column = self.cursor_location
        if row == len(lines) - 1 and column >= len(lines[-1]):
            if getattr(self.app, "focus_agent_list", lambda: False)():
                return
        super().action_cursor_down(select)

    def action_cursor_up(self, select: bool = False) -> None:
        if getattr(self.app, "completion_visible", False):
            self.app.select_previous_completion()
            return
        location = self.cursor_location
        if not select and self.navigator.is_first_wrapped_line(location):
            if location == (0, 0):
                # 已在全文开头：进入历史回溯
                if getattr(self.app, "history_prev", lambda: False)():
                    return
            else:
                # 第一视觉行但不在全文开头：先跳到全文开头
                self.move_cursor((0, 0))
                return
        super().action_cursor_up(select)


class AgentList(ListView):
    """底部 Agent 列表。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    BINDINGS = [
        *ListView.BINDINGS,
        Binding("escape", "return_to_input", show=False, priority=True),
    ]

    def action_return_to_input(self) -> None:
        getattr(self.app, "focus_composer", lambda: None)()

    def action_cursor_up(self) -> None:
        if self.index is None or self.index <= 0:
            self.action_return_to_input()
            return
        super().action_cursor_up()


class TranscriptPanel(PointerScrollMixin, VerticalScroll, can_focus=True):
    """可分页、切换 Agent 的只读转录面板。"""

    FOCUS_ON_CLICK = False

    BINDINGS = [
        Binding("up", "page_up", show=False, priority=True),
        Binding("down", "page_down", show=False, priority=True),
        Binding("left", "previous_agent", show=False, priority=True),
        Binding("right", "next_agent", show=False, priority=True),
        Binding("escape", "close_transcript", show=False, priority=True),
    ]

    def action_page_up(self) -> None:
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_previous_agent(self) -> None:
        getattr(self.app, "switch_transcript", lambda _delta: None)(-1)

    def action_next_agent(self) -> None:
        getattr(self.app, "switch_transcript", lambda _delta: None)(1)

    def action_close_transcript(self) -> None:
        getattr(self.app, "close_transcript", lambda: None)()


class NativeClipboard:
    """OSC 52 之外的 macOS/Windows 系统剪贴板后端。"""

    def __init__(self, platform_name: str) -> None:
        self.platform_name = platform_name

    @property
    def supported(self) -> bool:
        return self.platform_name in {"darwin", "win32"}

    def copy(self, text: str) -> bool:
        if self.platform_name == "darwin":
            command = ["pbcopy"]
        elif self.platform_name == "win32":
            command = ["clip.exe"]
        else:
            return False
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=clean_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    async def copy_async(self, text: str) -> bool:
        return await asyncio.to_thread(self.copy, text)
