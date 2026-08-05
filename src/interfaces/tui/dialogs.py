"""Textual 交互窗口与 FIFO 请求协调器。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.markdown import Markdown as RichMarkdown
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea
from textual.widgets.option_list import Option

from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    MenuRequest,
    PermissionMenu,
    TranscriptView,
    UiRequest,
    ViewRequest,
)
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.widgets import (
    KeyboardNavigation,
    KeyboardOptionList,
    KeyboardTextArea,
)

if TYPE_CHECKING:
    from src.interfaces.tui.app import AgentTuiApp


@dataclass(frozen=True, slots=True)
class DialogResult:
    """一个 Modal 的结束结果。"""

    value: str = ""
    cancelled: bool = False


@dataclass(slots=True)
class PendingInteractions:
    """TUI 降级到文字模式时转交的未完成请求。"""

    active: MenuRequest | None
    queue: list[MenuRequest]
    view_request: ViewRequest | None


class KeyboardDialog(ModalScreen[DialogResult]):
    """拥有唯一键盘焦点目标的交互窗口。"""

    def restore_focus(self) -> None:
        raise NotImplementedError


def _prompt_widget(request: MenuRequest):
    prompt = getattr(request, "prompt", "")
    if getattr(request, "markdown", False):
        return Markdown(prompt, classes="dialog-prompt")
    return Static(prompt, classes="dialog-prompt", markup=False)


def _source_label(request: UiRequest) -> str:
    agent_type = request.caller_agent_type
    if not agent_type:
        return request.source
    short_uuid = request.caller_uuid.split("-")[0] if request.caller_uuid else ""
    return f"{agent_type} {short_uuid}" if short_uuid else agent_type


def _option_prompt(index: int, label: str, markdown: bool) -> object:
    text = f"{index}. {label}"
    return RichMarkdown(text) if markdown else text


class SelectionDialog(KeyboardDialog):
    """权限与普通单选窗口。"""

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: PermissionMenu | ChoiceMenu) -> None:
        super().__init__()
        self.request = request

    @property
    def options(self) -> list[tuple[str, str]]:
        if isinstance(self.request, PermissionMenu):
            return [("yes", "允许一次"), ("deny", "拒绝 (esc)")]
        return self.request.options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-shell", classes=f"dialog-{self.request.type}"):
            title = "工具请求权限" if isinstance(self.request, PermissionMenu) else "请选择"
            yield Static(title, classes="dialog-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield Static(f"发起 Agent  {source}", classes="dialog-source", markup=False)
            if isinstance(self.request, PermissionMenu):
                yield Static(
                    f"工具  {self.request.tool_name}\n内容  {self.request.detail}",
                    classes="dialog-detail",
                    markup=False,
                )
            elif self.request.prompt:
                yield _prompt_widget(self.request)
            yield KeyboardOptionList(
                *[
                    Option(_option_prompt(
                        index,
                        label,
                        getattr(self.request, "markdown", False),
                    ))
                    for index, (_value, label) in enumerate(self.options, 1)
                ],
                id="dialog-options",
                markup=False,
            )

    def on_mount(self) -> None:
        options = self.query_one(KeyboardOptionList)
        options.highlighted = min(
            max(0, getattr(self.request, "default_index", 0)),
            max(0, len(self.options) - 1),
        )
        self.restore_focus()

    def restore_focus(self) -> None:
        self.query_one(KeyboardOptionList).focus()

    def on_option_list_option_selected(
        self,
        event: KeyboardOptionList.OptionSelected,
    ) -> None:
        self._choose(event.option_index)

    def action_choose_number(self, index: int) -> None:
        self._choose(index - 1)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self.options):
            self.dismiss(DialogResult(value=self.options[index][0]))

    def action_cancel(self) -> None:
        if isinstance(self.request, PermissionMenu):
            self.dismiss(DialogResult(value="deny"))
        else:
            self.dismiss(DialogResult(cancelled=True))


class InlineWidget(Vertical, can_focus=True):
    """内嵌到聊天历史流中的交互基类。"""

    FOCUS_ON_CLICK = False
    ALLOW_SELECT = False

    class Completed(Message):
        """交互完成时发送。"""

        def __init__(self, result: DialogResult) -> None:
            super().__init__()
            self.result = result

    def __init__(self, **kwargs) -> None:
        super().__init__(classes="inline-widget", **kwargs)
        self._completed = False

    def build_summary(self) -> str:
        """构建完成后的摘要文本。"""
        raise NotImplementedError

    def restore_focus(self) -> None:
        """恢复焦点到合适的子组件。"""
        raise NotImplementedError


class InlineSelectionWidget(InlineWidget):
    """内嵌权限确认与单选菜单。"""

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: PermissionMenu | ChoiceMenu) -> None:
        super().__init__()
        self.request = request
        self._chosen_label = ""

    @property
    def options(self) -> list[tuple[str, str]]:
        if isinstance(self.request, PermissionMenu):
            return [("yes", "允许一次"), ("deny", "拒绝 (esc)")]
        return self.request.options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-shell", classes=f"dialog-{self.request.type}"):
            title = "工具请求权限" if isinstance(self.request, PermissionMenu) else "请选择"
            yield Static(title, classes="dialog-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield Static(f"发起 Agent  {source}", classes="dialog-source", markup=False)
            if isinstance(self.request, PermissionMenu):
                yield Static(
                    f"工具  {self.request.tool_name}\n内容  {self.request.detail}",
                    classes="dialog-detail",
                    markup=False,
                )
            elif self.request.prompt:
                yield _prompt_widget(self.request)
            yield KeyboardOptionList(
                *[
                    Option(_option_prompt(
                        index,
                        label,
                        getattr(self.request, "markdown", False),
                    ))
                    for index, (_value, label) in enumerate(self.options, 1)
                ],
                id="dialog-options",
                markup=False,
            )

    def on_mount(self) -> None:
        options = self.query_one(KeyboardOptionList)
        options.highlighted = min(
            max(0, getattr(self.request, "default_index", 0)),
            max(0, len(self.options) - 1),
        )
        self.restore_focus()

    def restore_focus(self) -> None:
        if self._completed:
            return
        self.query_one(KeyboardOptionList).focus()

    def on_option_list_option_selected(
        self,
        event: KeyboardOptionList.OptionSelected,
    ) -> None:
        self._choose(event.option_index)

    def action_choose_number(self, index: int) -> None:
        self._choose(index - 1)

    def _choose(self, index: int) -> None:
        if self._completed:
            return
        if 0 <= index < len(self.options):
            self._completed = True
            self._chosen_label = self.options[index][1]
            self.post_message(self.Completed(DialogResult(value=self.options[index][0])))

    def action_cancel(self) -> None:
        if self._completed:
            return
        self._completed = True
        if isinstance(self.request, PermissionMenu):
            self._chosen_label = "拒绝"
            self.post_message(self.Completed(DialogResult(value="deny")))
        else:
            self.post_message(self.Completed(DialogResult(cancelled=True)))

    def build_summary(self) -> str:
        if isinstance(self.request, PermissionMenu):
            return f"权限确认：{self.request.tool_name} → {self._chosen_label or '拒绝'}"
        return f"选择：{self._chosen_label or self.request.prompt or ''}"


class InlineChoiceInputWidget(InlineWidget):
    """内嵌选项与自由输入互斥的交互。"""

    BINDINGS = [
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: ChoiceInputMenu) -> None:
        super().__init__()
        self.request = request
        self.row = min(max(0, request.default_index), len(request.options))
        self._result_summary = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-shell", classes="dialog-choice-input"):
            yield Static("请选择或输入", classes="dialog-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield Static(f"发起 Agent  {source}", classes="dialog-source", markup=False)
            if self.request.prompt:
                yield _prompt_widget(self.request)
            yield KeyboardNavigation("", id="choice-input-options", markup=False)
            yield KeyboardTextArea(
                "",
                id="dialog-input",
                soft_wrap=True,
                show_line_numbers=False,
                placeholder=self.request.input_placeholder,
            )
            yield Static(
                "↑↓ 选择 · Enter/数字 确认 · Shift+Enter 换行 · Esc 取消",
                classes="dialog-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._render_rows()

    def _render_rows(self) -> None:
        lines: list[str] = []
        descriptions = self.request.descriptions or []
        for index, (_value, label) in enumerate(self.request.options):
            marker = "❯" if self.row == index else " "
            lines.append(f"{marker} {index + 1}. {label}")
            if index < len(descriptions) and descriptions[index]:
                lines.append(f"     {descriptions[index]}")
        marker = "❯" if self.row == len(self.request.options) else " "
        lines.append(f"{marker} ⌨ 自由输入")
        self.query_one("#choice-input-options", KeyboardNavigation).update(
            "\n".join(lines)
        )
        input_widget = self.query_one("#dialog-input", TextArea)
        input_widget.read_only = self.row != len(self.request.options)
        input_widget.show_cursor = not input_widget.read_only
        self.restore_focus()

    def restore_focus(self) -> None:
        if self._completed:
            return
        input_widget = self.query_one("#dialog-input", TextArea)
        if input_widget.read_only:
            self.query_one("#choice-input-options", KeyboardNavigation).focus()
        else:
            input_widget.focus()

    def action_move(self, delta: int) -> None:
        self.row = min(max(0, self.row + delta), len(self.request.options))
        self._render_rows()

    def action_choose_number(self, index: int) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.row == len(self.request.options) and not input_widget.read_only:
            input_widget.insert(str(index))
            return
        if 1 <= index <= len(self.request.options):
            self._submit_choice(self.request.options[index - 1][0])

    def action_submit(self) -> None:
        if self._completed:
            return
        if self.row < len(self.request.options):
            label = self.request.options[self.row][1]
            self._result_summary = f"选择：{label}"
            self._submit_choice(self.request.options[self.row][0])
            return
        text = self.query_one("#dialog-input", TextArea).text.strip()
        if text:
            self._completed = True
            self._result_summary = f"输入：{text[:60]}"
            self.post_message(self.Completed(DialogResult(value=json.dumps(
                {"choice": "", "text": text}, ensure_ascii=False
            ))))

    def action_newline(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if not input_widget.read_only:
            input_widget.insert("\n")

    def _submit_choice(self, value: str) -> None:
        if self._completed:
            return
        self._completed = True
        self.post_message(self.Completed(DialogResult(value=json.dumps(
            {"choice": value, "text": ""}, ensure_ascii=False
        ))))

    def action_cancel(self) -> None:
        if self._completed:
            return
        self._completed = True
        self.post_message(self.Completed(DialogResult(cancelled=True)))

    def build_summary(self) -> str:
        return self._result_summary or f"选择：{self.request.prompt or '选项输入'}"


class ChoiceInputDialog(KeyboardDialog):
    """选项与自由输入互斥的窗口。"""

    BINDINGS = [
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: ChoiceInputMenu) -> None:
        super().__init__()
        self.request = request
        self.row = min(max(0, request.default_index), len(request.options))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-shell", classes="dialog-choice-input"):
            yield Static("请选择或输入", classes="dialog-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield Static(f"发起 Agent  {source}", classes="dialog-source", markup=False)
            if self.request.prompt:
                yield _prompt_widget(self.request)
            yield KeyboardNavigation("", id="choice-input-options", markup=False)
            yield KeyboardTextArea(
                "",
                id="dialog-input",
                soft_wrap=True,
                show_line_numbers=False,
                placeholder=self.request.input_placeholder,
            )
            yield Static(
                "↑↓ 选择 · Enter/数字 确认 · Shift+Enter 换行 · Esc 取消",
                classes="dialog-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._render_rows()

    def _render_rows(self) -> None:
        lines: list[str] = []
        descriptions = self.request.descriptions or []
        for index, (_value, label) in enumerate(self.request.options):
            marker = "❯" if self.row == index else " "
            lines.append(f"{marker} {index + 1}. {label}")
            if index < len(descriptions) and descriptions[index]:
                lines.append(f"     {descriptions[index]}")
        marker = "❯" if self.row == len(self.request.options) else " "
        lines.append(f"{marker} ⌨ 自由输入")
        self.query_one("#choice-input-options", KeyboardNavigation).update(
            "\n".join(lines)
        )
        input_widget = self.query_one("#dialog-input", TextArea)
        input_widget.read_only = self.row != len(self.request.options)
        input_widget.show_cursor = not input_widget.read_only
        self.restore_focus()

    def restore_focus(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if input_widget.read_only:
            self.query_one("#choice-input-options", KeyboardNavigation).focus()
        else:
            input_widget.focus()

    def action_move(self, delta: int) -> None:
        self.row = min(max(0, self.row + delta), len(self.request.options))
        self._render_rows()

    def action_choose_number(self, index: int) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.row == len(self.request.options) and not input_widget.read_only:
            input_widget.insert(str(index))
            return
        if 1 <= index <= len(self.request.options):
            self._submit_choice(self.request.options[index - 1][0])

    def action_submit(self) -> None:
        if self.row < len(self.request.options):
            self._submit_choice(self.request.options[self.row][0])
            return
        text = self.query_one("#dialog-input", TextArea).text.strip()
        if text:
            self.dismiss(DialogResult(value=json.dumps(
                {"choice": "", "text": text}, ensure_ascii=False
            )))

    def action_newline(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if not input_widget.read_only:
            input_widget.insert("\n")

    def _submit_choice(self, value: str) -> None:
        self.dismiss(DialogResult(value=json.dumps({"choice": value, "text": ""})))

    def action_cancel(self) -> None:
        self.dismiss(DialogResult(cancelled=True))


class FormDialog(KeyboardDialog):
    """ask_user 多问题表单。"""

    BINDINGS = [
        Binding("left", "move_tab(-1)", show=False, priority=True),
        Binding("right", "move_tab(1)", show=False, priority=True),
        Binding("up", "move_row(-1)", show=False, priority=True),
        Binding("down", "move_row(1)", show=False, priority=True),
        Binding("space", "toggle", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("tab", "toggle_discussion", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: FormMenu) -> None:
        super().__init__()
        self.request = request
        self.tab = 0
        self.zone = "answer"
        self.rows = [0 for _ in request.questions]
        self.checked = [set() for _ in request.questions]
        self.custom = ["" for _ in request.questions]
        self.discussion = ""
        self._loading_input = False
        self._has_any_preview = any(q.has_previews for q in request.questions)

    def _current_has_previews(self) -> bool:
        """当前 tab 的问题是否应展示预览分栏。"""
        if self.tab >= len(self.request.questions):
            return False
        return self.request.questions[self.tab].has_previews

    def compose(self) -> ComposeResult:
        classes = "dialog-form dialog-form-preview" if self._has_any_preview else "dialog-form"
        with Vertical(id="dialog-shell", classes=classes):
            source = _source_label(self.request)
            if source:
                yield Static(
                    f"[#efc36a bold]问题[/]  ·  {source}",
                    classes="dialog-title",
                )
            else:
                yield Static("问题", classes="dialog-title", markup=False)
            yield Static("", id="form-tabs", markup=False)
            if self._has_any_preview:
                yield Static("", id="form-question-text", markup=False)
                with Horizontal(id="form-split"):
                    with Vertical(id="form-left"):
                        yield KeyboardNavigation("", id="form-body", markup=False)
                        yield KeyboardTextArea(
                            "",
                            id="dialog-input",
                            soft_wrap=True,
                            show_line_numbers=False,
                            placeholder="输入自定义回答…",
                        )
                    with VerticalScroll(id="form-preview-pane"):
                        yield Markdown("", id="form-preview")
            else:
                yield KeyboardNavigation("", id="form-body", markup=False)
                yield KeyboardTextArea(
                    "",
                    id="dialog-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                    placeholder="输入自定义回答…",
                )
            yield Static("", id="form-hint", classes="dialog-hint", markup=False)

    def on_mount(self) -> None:
        self._render_form()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._loading_input:
            return
        if self.zone == "discussion":
            self.discussion = event.text_area.text
        elif self.tab < len(self.request.questions):
            question = self.request.questions[self.tab]
            if self.rows[self.tab] == self._option_count(question):
                self.custom[self.tab] = event.text_area.text
        self._render_form(update_input=False)

    @staticmethod
    def _option_count(question: FormQuestion) -> int:
        return len(question.options or [])

    def _answer(self, index: int) -> str:
        question = self.request.questions[index]
        options = question.options
        if options is None:
            return self.custom[index]
        if question.multi_select:
            values = [options[item][0] for item in sorted(self.checked[index])]
            if self.custom[index].strip():
                values.append(self.custom[index].strip())
            return "、".join(values)
        if self.checked[index]:
            return options[min(self.checked[index])][0]
        return self.custom[index]

    def _render_form(self, *, update_input: bool = True) -> None:
        labels: list[str] = []
        for index, question in enumerate(self.request.questions):
            marker = "☑" if self._answer(index) else "☐"
            label = f"{marker} {question.header or f'问题{index + 1}'}"
            labels.append(f"[{label}]" if self.tab == index else label)
        labels.append("[提交]" if self.tab == len(self.request.questions) else "提交")
        self.query_one("#form-tabs", Static).update("  ".join(labels))

        if self.tab == len(self.request.questions):
            lines = ["回答小结"]
            for index in range(len(self.request.questions)):
                lines.append(f"问题{index + 1}：{self._answer(index) or '未作答'}")
        else:
            question = self.request.questions[self.tab]
            options = question.options or []
            descriptions = question.descriptions or []
            lines = [question.question]
            for index, (_value, label) in enumerate(options):
                selected = index in self.checked[self.tab]
                mark = (
                    "[x]" if question.multi_select and selected
                    else "[ ]" if question.multi_select
                    else "●" if selected
                    else "○"
                )
                cursor = "❯" if self.zone == "answer" and self.rows[self.tab] == index else " "
                lines.append(f"{cursor} {mark} {index + 1}. {label}")
                # 有 preview 时不展示 description 副行（预览区已提供详细信息）
                if not self._current_has_previews() and index < len(descriptions) and descriptions[index]:
                    lines.append(f"      {descriptions[index]}")
            custom_cursor = (
                "❯" if self.zone == "answer" and self.rows[self.tab] == len(options) else " "
            )
            lines.append(f"{custom_cursor} ⌨ 其他: {self.custom[self.tab] or '输入回答…'}")

        # 有预览分栏时，问题文本全宽显示、选项列表进分栏左侧
        if self._has_any_preview:
            question_text_widget = self.query_one("#form-question-text", Static)
            if self.tab < len(self.request.questions):
                question_text_widget.update(lines[0])
                self.query_one("#form-body", KeyboardNavigation).update(
                    "\n".join(lines[1:])
                )
            else:
                question_text_widget.update("")
                self.query_one("#form-body", KeyboardNavigation).update(
                    "\n".join(lines)
                )
        else:
            self.query_one("#form-body", KeyboardNavigation).update("\n".join(lines))

        input_widget = self.query_one("#dialog-input", TextArea)
        editable = self._input_editable()
        input_widget.display = editable
        input_widget.read_only = not editable
        input_widget.show_cursor = editable
        if update_input and editable:
            value = self.discussion if self.zone == "discussion" else self.custom[self.tab]
            self._loading_input = True
            input_widget.load_text(value)
            input_widget.move_cursor((len(value.split("\n")) - 1, len(value.split("\n")[-1])))
            self._loading_input = False
        if editable:
            input_widget.placeholder = (
                "讨论这几个问题…" if self.zone == "discussion" else "输入自定义回答…"
            )

        # --- 预览窗格更新 ---
        if self._has_any_preview:
            pane = self.query_one("#form-preview-pane", VerticalScroll)
            preview_widget = self.query_one("#form-preview", Markdown)
            if self._current_has_previews():
                question = self.request.questions[self.tab]
                previews = question.previews or []
                row = self.rows[self.tab]
                if row < len(previews) and previews[row].strip():
                    preview_widget.update(previews[row])
                    pane.display = True
                else:
                    pane.display = False
                pane.scroll_home(animate=False)
            else:
                pane.display = False

        self.restore_focus()
        hint = (
            "Tab 返回答题 · Enter 提交 · Shift+Enter 换行 · Esc 取消"
            if self.zone == "discussion"
            else "↑↓ 选择 · ←→ 切换问题 · Space/数字 选择 · Tab 讨论 · Esc 取消"
        )
        self.query_one("#form-hint", Static).update(hint)

    def _input_editable(self) -> bool:
        return self.zone == "discussion" or (
            self.tab < len(self.request.questions)
            and self.rows[self.tab]
            == self._option_count(self.request.questions[self.tab])
        )

    def restore_focus(self) -> None:
        if self._input_editable():
            self.query_one("#dialog-input", TextArea).focus()
        else:
            self.query_one("#form-body", KeyboardNavigation).focus()

    def _commit_input(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            self.discussion = input_widget.text
        elif (
            self.tab < len(self.request.questions)
            and self.rows[self.tab]
            == self._option_count(self.request.questions[self.tab])
            and input_widget.display
            and not input_widget.read_only
        ):
            self.custom[self.tab] = input_widget.text
            question = self.request.questions[self.tab]
            if question.options is not None and not question.multi_select and input_widget.text.strip():
                self.checked[self.tab].clear()

    def action_move_tab(self, delta: int) -> None:
        if self.zone != "answer":
            return
        self._commit_input()
        self.tab = min(max(0, self.tab + delta), len(self.request.questions))
        self._render_form()

    def action_move_row(self, delta: int) -> None:
        if self.zone != "answer" or self.tab >= len(self.request.questions):
            return
        self._commit_input()
        maximum = self._option_count(self.request.questions[self.tab])
        self.rows[self.tab] = min(max(0, self.rows[self.tab] + delta), maximum)
        self._render_form()

    def action_toggle(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            input_widget.insert(" ")
            return
        if self.tab >= len(self.request.questions):
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        row = self.rows[self.tab]
        if row >= len(options):
            if input_widget.display and not input_widget.read_only:
                input_widget.insert(" ")
            return
        if question.multi_select:
            if row in self.checked[self.tab]:
                self.checked[self.tab].remove(row)
            else:
                self.checked[self.tab].add(row)
        elif row in self.checked[self.tab]:
            self.checked[self.tab].clear()
        else:
            self.checked[self.tab] = {row}
            self.custom[self.tab] = ""
        self._render_form(update_input=False)

    def action_choose_number(self, index: int) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            input_widget.insert(str(index))
            return
        if self.tab >= len(self.request.questions):
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        if self.rows[self.tab] == len(options) and not input_widget.read_only:
            input_widget.insert(str(index))
            return
        if 1 <= index <= len(options):
            self.rows[self.tab] = index - 1
            self.action_toggle()

    def action_confirm(self) -> None:
        self._commit_input()
        if self.zone == "discussion" or self.tab == len(self.request.questions):
            self._submit()
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        if options and not question.multi_select and self.rows[self.tab] < len(options):
            self.action_toggle()
            return
        self.tab = min(self.tab + 1, len(self.request.questions))
        self._render_form()

    def action_toggle_discussion(self) -> None:
        self._commit_input()
        self.zone = "answer" if self.zone == "discussion" else "discussion"
        self._render_form()

    def action_newline(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if input_widget.display and not input_widget.read_only:
            input_widget.insert("\n")

    def _submit(self) -> None:
        payload = json.dumps(
            {
                "answers": [self._answer(index) for index in range(len(self.request.questions))],
                "discussion": self.discussion.strip(),
            },
            ensure_ascii=False,
        )
        self.dismiss(DialogResult(value=payload))

    def action_cancel(self) -> None:
        self.dismiss(DialogResult(cancelled=True))


class InlineFormWidget(InlineWidget):
    """内嵌到聊天历史流中的 ask_user 多问题表单。

    交互逻辑与 FormDialog 完全一致（tab 切换、选项选择、讨论等），
    但以普通 Widget 挂载到 HistoryPanel 中，而非 ModalScreen 弹窗。
    完成时发送 Completed Message 而非 dismiss。
    """

    BINDINGS = [
        Binding("left", "move_tab(-1)", show=False, priority=True),
        Binding("right", "move_tab(1)", show=False, priority=True),
        Binding("up", "move_row(-1)", show=False, priority=True),
        Binding("down", "move_row(1)", show=False, priority=True),
        Binding("space", "toggle", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("tab", "toggle_discussion", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
        *[
            Binding(str(index), f"choose_number({index})", show=False, priority=True)
            for index in range(1, 10)
        ],
    ]

    def __init__(self, request: FormMenu) -> None:
        super().__init__()
        self.request = request
        self.tab = 0
        self.zone = "answer"
        self.rows = [0 for _ in request.questions]
        self.checked: list[set[int]] = [set() for _ in request.questions]
        self.custom = ["" for _ in request.questions]
        self.discussion = ""
        self._loading_input = False
        self._has_any_preview = any(q.has_previews for q in request.questions)

    def _current_has_previews(self) -> bool:
        if self.tab >= len(self.request.questions):
            return False
        return self.request.questions[self.tab].has_previews

    def compose(self) -> ComposeResult:
        classes = "dialog-form dialog-form-preview" if self._has_any_preview else "dialog-form"
        with Vertical(id="dialog-shell", classes=classes):
            source = _source_label(self.request)
            if source:
                yield Static(
                    f"[#efc36a bold]问题[/]  ·  {source}",
                    classes="dialog-title",
                )
            else:
                yield Static("问题", classes="dialog-title", markup=False)
            yield Static("", id="form-tabs", markup=False)
            if self._has_any_preview:
                yield Static("", id="form-question-text", markup=False)
                with Horizontal(id="form-split"):
                    with Vertical(id="form-left"):
                        yield KeyboardNavigation("", id="form-body", markup=False)
                        yield KeyboardTextArea(
                            "",
                            id="dialog-input",
                            soft_wrap=True,
                            show_line_numbers=False,
                            placeholder="输入自定义回答…",
                        )
                    with VerticalScroll(id="form-preview-pane"):
                        yield Markdown("", id="form-preview")
            else:
                yield KeyboardNavigation("", id="form-body", markup=False)
                yield KeyboardTextArea(
                    "",
                    id="dialog-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                    placeholder="输入自定义回答…",
                )
            yield Static("", id="form-hint", classes="dialog-hint", markup=False)

    def on_mount(self) -> None:
        self._render_form()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._loading_input:
            return
        if self.zone == "discussion":
            self.discussion = event.text_area.text
        elif self.tab < len(self.request.questions):
            question = self.request.questions[self.tab]
            if self.rows[self.tab] == self._option_count(question):
                self.custom[self.tab] = event.text_area.text
        self._render_form(update_input=False)

    @staticmethod
    def _option_count(question: FormQuestion) -> int:
        return len(question.options or [])

    def _answer(self, index: int) -> str:
        question = self.request.questions[index]
        options = question.options
        if options is None:
            return self.custom[index]
        if question.multi_select:
            values = [options[item][0] for item in sorted(self.checked[index])]
            if self.custom[index].strip():
                values.append(self.custom[index].strip())
            return "、".join(values)
        if self.checked[index]:
            return options[min(self.checked[index])][0]
        return self.custom[index]

    def _render_form(self, *, update_input: bool = True) -> None:
        labels: list[str] = []
        for index, question in enumerate(self.request.questions):
            marker = "☑" if self._answer(index) else "☐"
            label = f"{marker} {question.header or f'问题{index + 1}'}"
            labels.append(f"[{label}]" if self.tab == index else label)
        labels.append("[提交]" if self.tab == len(self.request.questions) else "提交")
        self.query_one("#form-tabs", Static).update("  ".join(labels))

        if self.tab == len(self.request.questions):
            lines = ["回答小结"]
            for index in range(len(self.request.questions)):
                lines.append(f"问题{index + 1}：{self._answer(index) or '未作答'}")
        else:
            question = self.request.questions[self.tab]
            options = question.options or []
            descriptions = question.descriptions or []
            lines = [question.question]
            for index, (_value, label) in enumerate(options):
                selected = index in self.checked[self.tab]
                mark = (
                    "[x]" if question.multi_select and selected
                    else "[ ]" if question.multi_select
                    else "●" if selected
                    else "○"
                )
                cursor = "❯" if self.zone == "answer" and self.rows[self.tab] == index else " "
                lines.append(f"{cursor} {mark} {index + 1}. {label}")
                if not self._current_has_previews() and index < len(descriptions) and descriptions[index]:
                    lines.append(f"      {descriptions[index]}")
            custom_cursor = (
                "❯" if self.zone == "answer" and self.rows[self.tab] == len(options) else " "
            )
            lines.append(f"{custom_cursor} ⌨ 其他: {self.custom[self.tab] or '输入回答…'}")

        if self._has_any_preview:
            question_text_widget = self.query_one("#form-question-text", Static)
            if self.tab < len(self.request.questions):
                question_text_widget.update(lines[0])
                self.query_one("#form-body", KeyboardNavigation).update(
                    "\n".join(lines[1:])
                )
            else:
                question_text_widget.update("")
                self.query_one("#form-body", KeyboardNavigation).update(
                    "\n".join(lines)
                )
        else:
            self.query_one("#form-body", KeyboardNavigation).update("\n".join(lines))

        input_widget = self.query_one("#dialog-input", TextArea)
        editable = self._input_editable()
        input_widget.display = editable
        input_widget.read_only = not editable
        input_widget.show_cursor = editable
        if update_input and editable:
            value = self.discussion if self.zone == "discussion" else self.custom[self.tab]
            self._loading_input = True
            input_widget.load_text(value)
            input_widget.move_cursor((len(value.split("\n")) - 1, len(value.split("\n")[-1])))
            self._loading_input = False
        if editable:
            input_widget.placeholder = (
                "讨论这几个问题…" if self.zone == "discussion" else "输入自定义回答…"
            )

        if self._has_any_preview:
            pane = self.query_one("#form-preview-pane", VerticalScroll)
            preview_widget = self.query_one("#form-preview", Markdown)
            if self._current_has_previews():
                question = self.request.questions[self.tab]
                previews = question.previews or []
                row = self.rows[self.tab]
                if row < len(previews) and previews[row].strip():
                    preview_widget.update(previews[row])
                    pane.display = True
                else:
                    pane.display = False
                pane.scroll_home(animate=False)
            else:
                pane.display = False

        self.restore_focus()
        hint = (
            "Tab 返回答题 · Enter 提交 · Shift+Enter 换行 · Esc 取消"
            if self.zone == "discussion"
            else "↑↓ 选择 · ←→ 切换问题 · Space/数字 选择 · Tab 讨论 · Esc 取消"
        )
        self.query_one("#form-hint", Static).update(hint)

    def _input_editable(self) -> bool:
        return self.zone == "discussion" or (
            self.tab < len(self.request.questions)
            and self.rows[self.tab]
            == self._option_count(self.request.questions[self.tab])
        )

    def restore_focus(self) -> None:
        if self._completed:
            return
        if self._input_editable():
            self.query_one("#dialog-input", TextArea).focus()
        else:
            self.query_one("#form-body", KeyboardNavigation).focus()

    def _commit_input(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            self.discussion = input_widget.text
        elif (
            self.tab < len(self.request.questions)
            and self.rows[self.tab]
            == self._option_count(self.request.questions[self.tab])
            and input_widget.display
            and not input_widget.read_only
        ):
            self.custom[self.tab] = input_widget.text
            question = self.request.questions[self.tab]
            if question.options is not None and not question.multi_select and input_widget.text.strip():
                self.checked[self.tab].clear()

    def action_move_tab(self, delta: int) -> None:
        if self.zone != "answer":
            return
        self._commit_input()
        self.tab = min(max(0, self.tab + delta), len(self.request.questions))
        self._render_form()

    def action_move_row(self, delta: int) -> None:
        if self.zone != "answer" or self.tab >= len(self.request.questions):
            return
        self._commit_input()
        maximum = self._option_count(self.request.questions[self.tab])
        self.rows[self.tab] = min(max(0, self.rows[self.tab] + delta), maximum)
        self._render_form()

    def action_toggle(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            input_widget.insert(" ")
            return
        if self.tab >= len(self.request.questions):
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        row = self.rows[self.tab]
        if row >= len(options):
            if input_widget.display and not input_widget.read_only:
                input_widget.insert(" ")
            return
        if question.multi_select:
            if row in self.checked[self.tab]:
                self.checked[self.tab].remove(row)
            else:
                self.checked[self.tab].add(row)
        elif row in self.checked[self.tab]:
            self.checked[self.tab].clear()
        else:
            self.checked[self.tab] = {row}
            self.custom[self.tab] = ""
            # 单选选中后自动前进到下一个问题
            self.tab = min(self.tab + 1, len(self.request.questions))
        self._render_form(update_input=False)

    def action_choose_number(self, index: int) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if self.zone == "discussion":
            input_widget.insert(str(index))
            return
        if self.tab >= len(self.request.questions):
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        if self.rows[self.tab] == len(options) and not input_widget.read_only:
            input_widget.insert(str(index))
            return
        if 1 <= index <= len(options):
            self.rows[self.tab] = index - 1
            self.action_toggle()

    def action_confirm(self) -> None:
        self._commit_input()
        if self.zone == "discussion" or self.tab == len(self.request.questions):
            self._submit()
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        if options and not question.multi_select and self.rows[self.tab] < len(options):
            self.action_toggle()
            return
        self.tab = min(self.tab + 1, len(self.request.questions))
        self._render_form()

    def action_toggle_discussion(self) -> None:
        self._commit_input()
        self.zone = "answer" if self.zone == "discussion" else "discussion"
        self._render_form()

    def action_newline(self) -> None:
        input_widget = self.query_one("#dialog-input", TextArea)
        if input_widget.display and not input_widget.read_only:
            input_widget.insert("\n")

    def _submit(self) -> None:
        if self._completed:
            return
        self._completed = True
        payload = json.dumps(
            {
                "answers": [self._answer(index) for index in range(len(self.request.questions))],
                "discussion": self.discussion.strip(),
            },
            ensure_ascii=False,
        )
        self.post_message(self.Completed(DialogResult(value=payload)))

    def action_cancel(self) -> None:
        if self._completed:
            return
        self._completed = True
        self.post_message(self.Completed(DialogResult(cancelled=True)))

    def build_summary(self) -> str:
        """构建提交后的回答摘要文本。"""
        parts: list[str] = []
        for index, question in enumerate(self.request.questions):
            header = question.header or f"问题{index + 1}"
            answer = self._answer(index) or "未作答"
            parts.append(f"  {header} → {answer}")
        lines = ["用户选择："] + parts
        if self.discussion.strip():
            lines.append(f"讨论：{self.discussion.strip()}")
        return "\n".join(lines)


def make_dialog(request: MenuRequest) -> KeyboardDialog:
    if isinstance(request, FormMenu):
        return FormDialog(request)
    if isinstance(request, ChoiceInputMenu):
        return ChoiceInputDialog(request)
    if isinstance(request, (PermissionMenu, ChoiceMenu)):
        return SelectionDialog(request)
    raise TypeError(f"unsupported modal request: {type(request)!r}")


def _make_inline_widget(request: MenuRequest) -> InlineWidget:
    if isinstance(request, FormMenu):
        return InlineFormWidget(request)
    if isinstance(request, ChoiceInputMenu):
        return InlineChoiceInputWidget(request)
    if isinstance(request, (PermissionMenu, ChoiceMenu)):
        return InlineSelectionWidget(request)
    raise TypeError(f"unsupported inline request: {type(request)!r}")


class InteractionCoordinator:
    """管理普通输入、Modal FIFO、Transcript 和请求 Future 生命周期。"""

    def __init__(self, app: AgentTuiApp, turn_clock: TurnClock) -> None:
        self.app = app
        self.turn_clock = turn_clock
        self.active: MenuRequest | None = None
        self.queue: deque[MenuRequest] = deque()
        self.view_request: ViewRequest | None = None
        self.modal: KeyboardDialog | None = None
        self.inline_widget: InlineWidget | None = None
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._detached = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def modal_active(self) -> bool:
        return self.modal is not None

    @property
    def inline_widget_active(self) -> bool:
        return self.inline_widget is not None

    @property
    def input_active(self) -> bool:
        return isinstance(self.active, InputMenu)

    @property
    def pending_summary(self) -> tuple[int, str | None]:
        self._drop_finished_queue_items()
        if not self.queue:
            return 0, None
        request = self.queue[0]
        return len(self.queue), request.caller_agent_type or request.source

    @property
    def is_idle(self) -> bool:
        return self.active is None and not self.queue and not self._cleanup_tasks

    async def submit(self, request: UiRequest) -> bool:
        if request.future is None or request.future.done():
            return False
        if self._closed or self._detached:
            request.cancel()
            return False
        request.future.add_done_callback(
            lambda _future, item=request: self._schedule(self._request_settled(item))
        )
        if isinstance(request, ViewRequest):
            await self._open_view(request)
            return True
        if not isinstance(request, MenuRequest):
            raise TypeError(f"unsupported UI request: {type(request)!r}")
        self.queue.append(request)
        self._idle.clear()
        await self._pump()
        self.app.refresh_chrome()
        return True

    async def _pump(self) -> None:
        if self._closed or self.active is not None:
            self._sync_idle()
            return
        self._drop_finished_queue_items()
        if not self.queue:
            self._sync_idle()
            return
        request = self.queue.popleft()
        self.active = request
        await self.app.record_request_context(request)
        if isinstance(request, InputMenu):
            await self.app.begin_input(request)
            self.app.refresh_chrome()
            return
        self.turn_clock.enter_human_wait()
        try:
            self.inline_widget = _make_inline_widget(request)
            await self.app.mount_inline_widget(self.inline_widget)
        except BaseException as exc:
            self.turn_clock.exit_human_wait()
            self.active = None
            self.inline_widget = None
            request.fail(exc)
            await self._pump()
            raise
        self.app.refresh_chrome()

    async def complete_input(self, text: str) -> bool:
        request = self.active
        if not isinstance(request, InputMenu) or not text.strip():
            return False
        await self.app.finish_input(text)
        self.active = None
        request.complete(text)
        await self._pump()
        return True

    def cancel_input_for_exit(self) -> bool:
        request = self.active
        if not isinstance(request, InputMenu):
            return False
        self.app.finish_input_cancelled()
        self.active = None
        request.cancel()
        self._schedule(self._pump())
        return True

    def _modal_finished(self, result: DialogResult | None) -> None:
        self._schedule(self._finish_modal(result or DialogResult(cancelled=True)))

    async def _finish_modal(self, result: DialogResult) -> None:
        request = self.active
        if request is None or isinstance(request, InputMenu):
            return
        self.modal = None
        self.app.set_screen_class(False, "dialog-open")
        self.turn_clock.exit_human_wait()
        self.active = None
        await asyncio.sleep(0)
        if request.future is not None and not request.future.done():
            if result.cancelled:
                request.complete("")
            else:
                request.complete(result.value)
        await self._pump()
        self.app.restore_focus()
        self.app.refresh_chrome()

    async def _finish_inline_widget(self, result: DialogResult) -> None:
        """内嵌交互完成后：移除 widget、追加摘要、结算 Future。"""
        request = self.active
        if request is None or isinstance(request, InputMenu):
            return
        widget = self.inline_widget
        self.inline_widget = None
        self.turn_clock.exit_human_wait()
        self.active = None
        # 移除 widget 并追加摘要
        if widget is not None and widget.is_mounted:
            summary = widget.build_summary() if not result.cancelled else ""
            await widget.remove()
            if result.cancelled:
                await self.app.append_output("[用户取消了作答]")
            else:
                await self.app.append_output(summary)
        await asyncio.sleep(0)
        if request.future is not None and not request.future.done():
            if result.cancelled:
                request.complete("")
            else:
                request.complete(result.value)
        await self._pump()
        self.app.restore_focus()
        self.app.refresh_chrome()

    async def _open_view(self, request: ViewRequest) -> None:
        if not isinstance(request, TranscriptView):
            raise TypeError(f"unsupported view request: {type(request)!r}")
        previous = self.view_request
        if previous is not None and previous is not request:
            previous.cancel()
        self.view_request = request
        opened = await self.app.open_transcript(
            request.uuid,
            [snapshot.uuid for snapshot in self.app.agent_view_store.subagent_snapshots()],
            invoked=True,
        )
        if not opened and self.view_request is request:
            self.view_request = None
            request.complete("")

    async def open_live_transcript(self, uuid: str) -> bool:
        if self._closed:
            return False
        ids = [
            snapshot.uuid
            for snapshot in self.app.agent_view_store.subagent_snapshots()
        ]
        return await self.app.open_transcript(uuid, ids, invoked=False)

    def close_transcript(self) -> bool:
        if self.app.viewing_agent_id is None:
            return False
        self.app.hide_transcript()
        request = self.view_request
        self.view_request = None
        if request is not None:
            request.complete("")
        self.app.restore_focus()
        return True

    def cancel_all(self, *, render: bool = True) -> bool:
        changed = bool(self.active or self.queue or self.view_request or self.app.viewing_agent_id)
        queued = list(self.queue)
        self.queue.clear()
        for request in queued:
            request.cancel()
        if isinstance(self.active, InputMenu):
            request = self.active
            self.active = None
            if render:
                self.app.finish_input_cancelled()
            request.cancel()
        elif self.active is not None:
            self.active.cancel()
            if self.inline_widget is not None:
                if render and self.inline_widget.is_mounted:
                    self._schedule(self.inline_widget.remove())
                self.inline_widget = None
            elif render and self.modal is not None and self.modal.is_current:
                self.modal.dismiss(DialogResult(cancelled=True))
            elif not render:
                self.modal = None
        if self.view_request is not None:
            self.view_request.cancel()
            self.view_request = None
        if render and self.app.viewing_agent_id is not None:
            self.app.hide_transcript()
        if not render:
            self.active = None
            self.modal = None
            self.inline_widget = None
            self.turn_clock.reset()
        self._sync_idle()
        if render:
            self.app.refresh_chrome()
        return changed

    async def wait_idle(self) -> None:
        while not self.is_idle:
            await self._idle.wait()
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        self.cancel_all()
        await self.wait_idle()

    def detach_for_fallback(self) -> PendingInteractions:
        """脱离已退出的 Textual app，并把未完成请求交给文字前端。"""
        snapshot = PendingInteractions(
            active=self.active,
            queue=list(self.queue),
            view_request=self.view_request,
        )
        self._detached = True
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        self.active = None
        self.queue.clear()
        self.view_request = None
        self.modal = None
        self.inline_widget = None
        self.turn_clock.reset()
        self._sync_idle()
        return snapshot

    def _render_available(self) -> bool:
        return bool(getattr(self.app, "is_running", False))

    async def _request_settled(self, request: UiRequest) -> None:
        if self._detached:
            return
        if request is self.view_request:
            self.view_request = None
            if self._render_available() and self.app.viewing_agent_id is not None:
                self.app.hide_transcript()
            return
        if request is self.active:
            if isinstance(request, InputMenu):
                if self._render_available():
                    self.app.finish_input_cancelled()
                self.active = None
                await self._pump()
            elif self.inline_widget is not None:
                if self._render_available() and self.inline_widget.is_mounted:
                    self.inline_widget.action_cancel()
            elif self._render_available() and self.modal is not None and self.modal.is_current:
                self.modal.dismiss(DialogResult(cancelled=True))
            return
        try:
            self.queue.remove(request)  # type: ignore[arg-type]
        except ValueError:
            return
        await self._pump()

    def reset(self) -> None:
        if self.is_idle and self.app.viewing_agent_id is not None:
            self.close_transcript()

    def _drop_finished_queue_items(self) -> None:
        self.queue = deque(
            request
            for request in self.queue
            if request.future is not None and not request.future.done()
        )

    def _schedule(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._cleanup_tasks.add(task)
        self._idle.clear()
        task.add_done_callback(self._cleanup_finished)

    def _cleanup_finished(self, task: asyncio.Task) -> None:
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._sync_idle()

    def _sync_idle(self) -> None:
        if self.is_idle:
            self._idle.set()
        else:
            self._idle.clear()
