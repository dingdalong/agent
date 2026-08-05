"""Textual TUI 的基础输入、滚动和列表组件。"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from functools import partial
from typing import Literal

from textual import events
from textual.binding import Binding
from textual.geometry import Offset
from textual.message import Message
from textual.screen import Screen
from textual.selection import SelectEnd, SelectStart, SelectState
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import ListItem, ListView, OptionList, Static, TextArea


_SELECT_AUTO_SCROLL_FPS = 20


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


class KeyboardTextArea(TextArea):
    """只接受键盘定位和编辑的文本输入框。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        event.prevent_default()
        event.stop()


class KeyboardOptionList(OptionList):
    """只接受键盘选择的选项列表。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    async def _on_click(self, event: events.Click) -> None:
        event.prevent_default()
        event.stop()


class KeyboardNavigation(Static, can_focus=True):
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
        row, column = self.cursor_location
        if row == 0 and not select:
            if column == 0:
                # 已在行首：进入历史回溯
                if getattr(self.app, "history_prev", lambda: False)():
                    return
            else:
                # 首行但非行首：先跳到行首
                self.move_cursor((0, 0))
                return
        super().action_cursor_up(select)


class HistoryPanel(VerticalScroll):
    """支持尾部跟随和跨视口稳定选择的历史区。"""

    FOCUS_ON_CLICK = False

    def on_mount(self) -> None:
        self.anchor()

    def on_mouse_down(self, _event: events.MouseDown) -> None:
        state = self.screen._select_state
        if state is None or state.end is not None:
            return
        start = state.start
        if start.container is not self and self not in start.container.ancestors:
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


class TranscriptPanel(VerticalScroll, can_focus=True):
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
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    async def copy_async(self, text: str) -> bool:
        return await asyncio.to_thread(self.copy, text)
