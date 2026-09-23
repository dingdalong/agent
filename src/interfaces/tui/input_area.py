"""主输入区：输入框形态、斜杠命令补全与输入历史回溯。

InputAreaController 持有输入框、补全菜单与输入区容器，集中本区状态（补全候选与下标、
历史回溯游标），并只读消费 App 的 coordinator / transcript / slash_commands /
get_input_history。跨区域焦点仲裁与轮次生命周期仍由 App 负责，App 与 Composer 组件
都通过 `input_area.*` 委托本控制器操作输入区。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical

from src.interfaces.tui.widgets import Composer, SelectionStatic

if TYPE_CHECKING:
    from src.interfaces.tui.app import AgentTuiApp

_MAX_COMPLETION_ROWS = 8
_MAX_COMPOSER_ROWS = 8


class InputAreaController:
    """渲染并维护主输入框、补全菜单及输入区高度与可见性。"""

    def __init__(self, app: AgentTuiApp) -> None:
        self._app = app
        self.shell = Vertical(id="composer-shell")
        self.composer = Composer(
            "",
            id="input",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder="输入消息或 / 命令…",
        )
        self.completion = SelectionStatic("", id="completion", markup=False)
        self._completion_matches: list[tuple[str, str]] = []
        self._completion_index = 0
        self._history_index: int | None = None
        self._history_draft = ""

    def start(self) -> None:
        """挂载后初始化只读态并同步高度（on_mount 调用一次）。"""
        self.composer.read_only = True
        self.composer.show_cursor = False
        self.resize()

    def focus(self) -> None:
        self.composer.focus()

    @property
    def text(self) -> str:
        return self.composer.text

    @property
    def read_only(self) -> bool:
        return self.composer.read_only

    def load_text(self, text: str, *, cursor_to_end: bool = False) -> None:
        """载入输入文本，把光标放到全文开头或末行末尾，并同步高度。"""
        self.composer.load_text(text)
        if cursor_to_end:
            lines = text.split("\n")
            self.composer.move_cursor((len(lines) - 1, len(lines[-1])))
        else:
            self.composer.move_cursor((0, 0))
        self.resize()

    def clear(self) -> None:
        """清空输入框、补全菜单并复位高度。"""
        self.composer.clear()
        self.resize()
        self.hide_completion()

    def resize(self) -> None:
        """按折行后的视觉行数设置输入区高度（1-8 行）。"""
        if not self.shell.is_mounted or not self.composer.is_mounted:
            return
        visual_lines = self.composer.wrapped_document.height
        self.shell.styles.height = min(_MAX_COMPOSER_ROWS, max(1, visual_lines))

    def sync_state(self) -> None:
        """同步只读态与光标可见性：转录查看与内嵌交互隐藏输入区并置为只读。

        处理态（Agent 工作中）也允许输入：提交内容进入排队区，由下一个输入窗口
        按序送出，因此这里不再要求 ``coordinator.input_active``。
        """
        if not self.composer.is_mounted:
            return
        app = self._app
        input_visible = (
            not app.transcript.viewing_agent_id
            and not app.coordinator.inline_widget_active
        )
        self.composer.read_only = not input_visible
        self.composer.show_cursor = input_visible

    def pull_last_queued_input(self) -> bool:
        """Alt+↑：取回最后一条排队输入并插到当前草稿之前。"""
        return self._app.pull_last_queued_input()

    def on_text_changed(self, text: str) -> None:
        """输入内容变化：先重算高度，再刷新补全候选。"""
        self.resize()
        self._refresh_completions(text)

    @property
    def completion_visible(self) -> bool:
        return bool(self._completion_matches)

    def render_completion(self) -> None:
        """按当前候选渲染补全菜单；无候选时隐藏。"""
        if not self.completion.is_mounted:
            return
        self.completion.display = self.completion_visible
        if not self.completion_visible:
            return
        lines = []
        for index, (name, description) in enumerate(
            self._completion_matches[:_MAX_COMPLETION_ROWS]
        ):
            marker = "❯" if index == self._completion_index else " "
            lines.append(f"{marker} /{name}  —  {description}")
        self.completion.update("\n".join(lines))

    def hide_completion(self) -> None:
        self._completion_matches = []
        self._completion_index = 0
        self.render_completion()

    def next_completion(self) -> bool:
        if not self.completion_visible:
            return False
        self._completion_index = (
            self._completion_index + 1
        ) % len(self._completion_matches)
        self.render_completion()
        return True

    def previous_completion(self) -> bool:
        if not self.completion_visible:
            return False
        self._completion_index = (
            self._completion_index - 1
        ) % len(self._completion_matches)
        self.render_completion()
        return True

    def apply_completion(self) -> bool:
        """把当前候选填入输入框；返回 False 表示没有待应用的补全。"""
        if not self.completion_visible:
            return False
        command = self._completion_matches[self._completion_index][0]
        self.load_text(f"/{command} ", cursor_to_end=True)
        self.hide_completion()
        return True

    def reset_history(self) -> None:
        """重置回溯游标（/clear、/resume 后调用）。

        历史本身不缓存：history_previous/next 每次直接从 provider 实时取，
        保证新提交的输入立即进入回溯，无需刷新快照。
        """
        self._history_index = None
        self._history_draft = ""

    def history_previous(self) -> bool:
        """上键：回溯到更早的一条输入历史。

        Returns:
            True 表示已消费该按键（载入了历史条目）；
            False 表示无历史或已在最早一条，交由光标正常上移。
        """
        history = self._app.get_input_history()
        if not history:
            return False
        if self._history_index is None:
            self._history_draft = self.composer.text
            self._history_index = len(history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return False
        self.load_text(history[self._history_index])
        return True

    def history_next(self) -> bool:
        """下键：前进到更新的一条输入历史，越过最新条时还原草稿。

        Returns:
            True 表示已消费该按键；False 表示当前不在回溯态。
        """
        if self._history_index is None:
            return False
        history = self._app.get_input_history()
        if self._history_index < len(history) - 1:
            self._history_index += 1
            self.load_text(history[self._history_index])
        else:
            self._history_index = None
            self.load_text(self._history_draft)
        return True

    def _refresh_completions(self, text: str) -> None:
        """仅当正在等待普通输入且内容是未加空格的斜杠前缀时给出候选。"""
        app = self._app
        stripped = text.lstrip()
        if (
            not stripped.startswith("/")
            or " " in stripped
            or not app.coordinator.input_active
            or app.transcript.viewing_agent_id
            or app.coordinator.inline_widget_active
        ):
            self.hide_completion()
            return
        prefix = stripped[1:].lower()
        self._completion_matches = [
            command for command in app.slash_commands
            if command[0].startswith(prefix)
        ]
        self._completion_index = min(
            self._completion_index,
            max(0, len(self._completion_matches) - 1),
        )
        self.render_completion()
