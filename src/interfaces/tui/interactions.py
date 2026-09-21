"""Textual 内嵌交互控件：把菜单请求渲染成聊天流内的表单。

控件只负责渲染与产出 `DialogResult`；排队、挂载与结算由 `coordinator` 负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Markdown, Static, TextArea
from textual.widgets.option_list import Option

from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    ModelMenu,
    MenuRequest,
    PermissionMenu,
    UiRequest,
)
from src.interfaces.status_presenter import agent_label
from src.interfaces.tui.widgets import (
    AutoGrowTextArea,
    FormBodyScroll,
    KeyboardNavigation,
    KeyboardOptionList,
    KeyboardTextArea,
    SelectionStatic,
)


@dataclass(frozen=True, slots=True)
class DialogResult:
    """一次交互请求的结束结果。"""

    value: str = ""
    cancelled: bool = False


def _prompt_widget(request: MenuRequest):
    prompt = getattr(request, "prompt", "")
    if getattr(request, "markdown", False):
        return Markdown(prompt, classes="inline-prompt")
    return SelectionStatic(prompt, classes="inline-prompt", markup=False)


def _source_label(request: UiRequest) -> str:
    return agent_label(request.caller_agent_type, request.caller_uuid) or request.source


def _form_source_label(request: UiRequest, task: str = "") -> str:
    """ask_user 表单标题行的来源标签：主 agent 不带短 UUID，子 agent 用任务摘要。"""
    agent_type = request.caller_agent_type
    if not agent_type:
        return request.source
    if agent_type == "main":
        return agent_type
    if task:
        return f"{agent_type}  {task}"
    return agent_label(agent_type, request.caller_uuid)


def _option_prompt(index: int, label: str, markdown: bool) -> object:
    text = f"{index}. {label}"
    return RichMarkdown(text) if markdown else text


_MODEL_MENU_SELECTION_STYLE = "#76d7c4"
_MODEL_MENU_SLOTS = ("default", "fast")


class _ModelMenuState:
    """模型菜单三轴选择状态：两个槽位各自的模型光标 + 角色级推理强度。"""

    def __init__(self, request: ModelMenu) -> None:
        """按请求的初始下标建立状态，并把越界下标收敛到合法范围。

        Args:
            request: 待作答的模型菜单请求。

        Returns:
            None。
        """
        last_model = max(0, len(request.models) - 1)
        self.request = request
        self.slot = 0
        self.model_indexes = [
            min(max(0, request.default_model_index), last_model),
            min(max(0, request.fast_model_index), last_model),
        ]
        self.effort_index = min(max(0, request.effort_index), max(0, len(request.efforts) - 1))

    @property
    def model_index(self) -> int:
        """当前激活槽位的模型光标下标（↑↓ 与滚动都以它为准）。"""
        return self.model_indexes[self.slot]

    def slot_labels(self, index: int) -> list[str]:
        """给出当前选择状态下指向该模型行的槽位名列表。

        Args:
            index: 模型行下标。

        Returns:
            指向该行的槽位名（如 ["default"]、["default", "fast"]）；无则为空列表。
        """
        return [
            name
            for slot, name in enumerate(_MODEL_MENU_SLOTS)
            if self.model_indexes[slot] == index
        ]

    def move_model(self, delta: int) -> bool:
        """在当前激活槽位内移动模型光标。

        Args:
            delta: 位移量（-1 上移、1 下移）。

        Returns:
            是否发生了可渲染的状态变化。
        """
        if not self.request.models:
            return False
        self.model_indexes[self.slot] = min(
            max(0, self.model_index + delta), len(self.request.models) - 1
        )
        return True

    def move_effort(self, delta: int) -> bool:
        """移动角色级推理强度光标（与激活槽位无关）。

        Args:
            delta: 位移量（-1 左移、1 右移）。

        Returns:
            是否发生了可渲染的状态变化。
        """
        if not self.request.efforts:
            return False
        self.effort_index = min(
            max(0, self.effort_index + delta), len(self.request.efforts) - 1
        )
        return True

    def toggle_slot(self) -> bool:
        """切换激活槽位（default ⇄ fast）；模型光标随之落到该槽位已选模型。

        Returns:
            恒为 True（切换总需重绘）。
        """
        self.slot = (self.slot + 1) % len(_MODEL_MENU_SLOTS)
        return True

    @property
    def submittable(self) -> bool:
        """模型与强度都有可选项时才允许提交。"""
        return bool(self.request.models and self.request.efforts)


def _model_menu_slots_text(state: _ModelMenuState) -> Text:
    """渲染槽位行，标出当前激活槽位。"""
    text = Text("槽位")
    for slot, name in enumerate(_MODEL_MENU_SLOTS):
        selected = slot == state.slot
        marker = "›" if selected else " "
        text.append("  ")
        text.append(
            f"{marker} {name}",
            style=_MODEL_MENU_SELECTION_STYLE if selected else None,
        )
    return text


def _model_menu_models_text(state: _ModelMenuState) -> Text:
    """渲染模型行：激活槽位光标行反显，各行按当前选择状态标注槽位。"""
    text = Text("模型")
    for index, (_value, label) in enumerate(state.request.models):
        selected = index == state.model_index
        marker = "›" if selected else " "
        labels = state.slot_labels(index)
        suffix = f"  [{', '.join(labels)}]" if labels else ""
        text.append("\n")
        text.append(
            f"{marker} {label}{suffix}",
            style=_MODEL_MENU_SELECTION_STYLE if selected else None,
        )
    return text


def _model_menu_effort_text(state: _ModelMenuState) -> Text:
    """渲染角色级推理强度行。"""
    text = Text("强度（角色级）")
    for index, value in enumerate(state.request.efforts):
        selected = index == state.effort_index
        marker = "›" if selected else " "
        text.append("  ")
        text.append(
            f"{marker} {value}",
            style=_MODEL_MENU_SELECTION_STYLE if selected else None,
        )
    return text


def _model_menu_payload(state: _ModelMenuState) -> str:
    """把三轴选择编码为 {"default", "fast", "reasoning_effort"} JSON 串。"""
    return json.dumps(
        {
            "default": state.request.models[state.model_indexes[0]][0],
            "fast": state.request.models[state.model_indexes[1]][0],
            "reasoning_effort": state.request.efforts[state.effort_index],
        },
        ensure_ascii=False,
    )


_MODEL_MENU_HINT = "↑↓ 模型 · ←→ 强度 · Tab 槽位 · Enter 应用 · Esc 取消"


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

    def build_summary(self, result: DialogResult) -> str:
        """构建完成后的摘要文本；空串表示不写入历史。"""
        del result
        return ""

    def restore_focus(self) -> None:
        """恢复焦点到合适的子组件。"""
        raise NotImplementedError

    def _finish(self, result: DialogResult) -> bool:
        """结算一次交互：重复触发不再二次提交，返回本次是否生效。"""
        if self._completed:
            return False
        self._completed = True
        self.post_message(self.Completed(result))
        return True

    def _cancel(self) -> bool:
        """取消当前交互：与提交共用同一份幂等结算。"""
        return self._finish(DialogResult(cancelled=True))


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
        with Vertical(id="inline-shell"):
            title = "工具请求权限" if isinstance(self.request, PermissionMenu) else "请选择"
            yield SelectionStatic(title, classes="inline-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield SelectionStatic(f"发起 Agent  {source}", classes="inline-source", markup=False)
            if isinstance(self.request, PermissionMenu):
                yield SelectionStatic(
                    f"工具  {self.request.tool_name}\n内容  {self.request.detail}",
                    classes="inline-detail",
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
                id="inline-options",
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
        if self._completed or not 0 <= index < len(self.options):
            return
        self._chosen_label = self.options[index][1]
        self._finish(DialogResult(value=self.options[index][0]))

    def action_cancel(self) -> None:
        if isinstance(self.request, PermissionMenu):
            if self._finish(DialogResult(value="deny")):
                self._chosen_label = "拒绝"
        else:
            self._cancel()

    def build_summary(self, result: DialogResult) -> str:
        if result.cancelled:
            return ""
        if isinstance(self.request, PermissionMenu):
            return f"权限确认：{self.request.tool_name} → {self._chosen_label or '拒绝'}"
        return f"选择：{self._chosen_label or self.request.prompt or ''}"


class InlineModelSelectionWidget(InlineWidget):
    """内嵌模型双槽位与推理强度的单屏三轴选择器。

    完成时发送 Completed Message，由 `InteractionCoordinator` 结算请求。
    """

    BINDINGS = [
        Binding("up", "move_model(-1)", show=False, priority=True),
        Binding("down", "move_model(1)", show=False, priority=True),
        Binding("left", "move_effort(-1)", show=False, priority=True),
        Binding("right", "move_effort(1)", show=False, priority=True),
        Binding("tab", "toggle_slot", show=False, priority=True),
        Binding("enter", "submit", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
    ]

    def __init__(self, request: ModelMenu) -> None:
        super().__init__()
        self.request = request
        self.state = _ModelMenuState(request)

    def compose(self) -> ComposeResult:
        with Vertical(id="inline-shell"):
            yield SelectionStatic("选择模型与推理强度", classes="inline-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield SelectionStatic(f"发起 Agent  {source}", classes="inline-source", markup=False)
            if self.request.prompt:
                yield _prompt_widget(self.request)
            yield SelectionStatic("", id="model-menu-slots", markup=False)
            with VerticalScroll(id="model-menu-scroll"):
                yield KeyboardNavigation("", id="model-menu-body", markup=False)
            yield SelectionStatic("", id="model-menu-effort", markup=False)
            yield SelectionStatic(_MODEL_MENU_HINT, classes="inline-hint", markup=False)

    def on_mount(self) -> None:
        self._render_menu()

    def _render_menu(self) -> None:
        """按当前三轴状态重绘槽位行、模型行与强度行，并把光标行滚入视野。"""
        self.query_one("#model-menu-slots", Static).update(
            _model_menu_slots_text(self.state)
        )
        self.query_one("#model-menu-body", KeyboardNavigation).update(
            _model_menu_models_text(self.state)
        )
        self.query_one("#model-menu-effort", Static).update(
            _model_menu_effort_text(self.state)
        )
        self.restore_focus()
        self.call_after_refresh(self._scroll_selected_model)

    def _scroll_selected_model(self) -> None:
        """把激活槽位的光标行滚入模型滚动区（首行是「模型」表头，故偏移 1）。"""
        self.query_one("#model-menu-scroll", VerticalScroll).scroll_to_region(
            Region(0, self.state.model_index + 1, 1, 1),
            animate=False,
            immediate=True,
        )

    def restore_focus(self) -> None:
        self.query_one("#model-menu-body", KeyboardNavigation).focus()

    def action_move_model(self, delta: int) -> None:
        """在当前激活槽位内移动模型光标。

        Args:
            delta: 位移量（-1 上移、1 下移）。

        Returns:
            None。
        """
        if self.state.move_model(delta):
            self._render_menu()

    def action_move_effort(self, delta: int) -> None:
        """移动角色级推理强度光标（与激活槽位无关）。

        Args:
            delta: 位移量（-1 左移、1 右移）。

        Returns:
            None。
        """
        if self.state.move_effort(delta):
            self._render_menu()

    def action_toggle_slot(self) -> None:
        """切换激活槽位（default ⇄ fast），光标随之跳到该槽位已选模型。"""
        if self.state.toggle_slot():
            self._render_menu()

    def action_submit(self) -> None:
        """一次提交三键 payload（default、fast、reasoning_effort）。"""
        if self.state.submittable:
            self._finish(DialogResult(value=_model_menu_payload(self.state)))

    def action_cancel(self) -> None:
        self._cancel()

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
        with Vertical(id="inline-shell"):
            yield SelectionStatic("请选择或输入", classes="inline-title", markup=False)
            source = _source_label(self.request)
            if source:
                yield SelectionStatic(f"发起 Agent  {source}", classes="inline-source", markup=False)
            if self.request.prompt:
                yield _prompt_widget(self.request)
            yield KeyboardNavigation("", id="choice-input-options", markup=False)
            yield KeyboardTextArea(
                "",
                id="inline-input",
                soft_wrap=True,
                show_line_numbers=False,
                placeholder=self.request.input_placeholder,
            )
            yield SelectionStatic(
                "↑↓ 选择 · Enter/数字 确认 · Shift+Enter 换行 · Esc 取消",
                classes="inline-hint",
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
        input_widget = self.query_one("#inline-input", TextArea)
        input_widget.read_only = self.row != len(self.request.options)
        input_widget.show_cursor = not input_widget.read_only
        self.restore_focus()

    def restore_focus(self) -> None:
        if self._completed:
            return
        input_widget = self.query_one("#inline-input", TextArea)
        if input_widget.read_only:
            self.query_one("#choice-input-options", KeyboardNavigation).focus()
        else:
            input_widget.focus()

    def action_move(self, delta: int) -> None:
        self.row = min(max(0, self.row + delta), len(self.request.options))
        self._render_rows()

    def action_choose_number(self, index: int) -> None:
        input_widget = self.query_one("#inline-input", TextArea)
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
        text = self.query_one("#inline-input", TextArea).text.strip()
        if text:
            self._result_summary = f"输入：{text[:60]}"
            self._finish(DialogResult(value=json.dumps(
                {"choice": "", "text": text}, ensure_ascii=False
            )))

    def action_newline(self) -> None:
        input_widget = self.query_one("#inline-input", TextArea)
        if not input_widget.read_only:
            input_widget.insert("\n")

    def _submit_choice(self, value: str) -> None:
        if self._completed:
            return
        self._finish(DialogResult(value=json.dumps(
            {"choice": value, "text": ""}, ensure_ascii=False
        )))

    def action_cancel(self) -> None:
        self._cancel()

    def build_summary(self, result: DialogResult) -> str:
        if result.cancelled:
            return ""
        return self._result_summary or f"选择：{self.request.prompt or '选项输入'}"


class InlineFormWidget(InlineWidget):
    """内嵌到聊天历史流中的 ask_user 多问题表单。

    结构化常驻 DOM：每题题干/选项/其它输入与共享讨论输入在挂载时一次性构建，
    交互只更新 marker、display、preview 与焦点，不重建组件——避免重渲染与延迟
    焦点交错导致的输入框光标丢失。完成时发送 Completed Message 而非 dismiss。
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

    def __init__(self, request: FormMenu, task: str = "") -> None:
        super().__init__()
        self.request = request
        self._task = task
        self.tab = 0
        self.zone = "answer"
        self.rows = [0 for _ in request.questions]
        self.checked: list[set[int]] = [set() for _ in request.questions]
        self.custom = ["" for _ in request.questions]
        self.discussion = ""
        self._has_any_preview = any(q.has_previews for q in request.questions)

    def _current_has_previews(self) -> bool:
        if self.tab >= len(self.request.questions):
            return False
        return self.request.questions[self.tab].has_previews

    def _content_widget(self, source: str, widget_id: str, classes: str) -> Widget:
        """按 markdown 开关生成表单内容组件。"""
        if self.request.markdown:
            return Markdown(source, id=widget_id, classes=classes)
        return SelectionStatic(source, id=widget_id, classes=classes, markup=False)

    def compose(self) -> ComposeResult:
        classes = "inline-form-preview" if self._has_any_preview else ""
        with Vertical(id="inline-shell", classes=classes):
            source = _form_source_label(self.request, self._task)
            if source:
                yield SelectionStatic(
                    f"[#efc36a bold]问题[/]  ·  {source}",
                    classes="inline-title",
                )
            else:
                yield SelectionStatic("问题", classes="inline-title", markup=False)
            yield SelectionStatic("", id="form-hint", classes="inline-hint", markup=False)
            yield SelectionStatic("", id="form-tabs", markup=False)
            for index, question in enumerate(self.request.questions):
                yield self._content_widget(
                    question.question,
                    f"question-text-{index}",
                    "form-question",
                )
            if self._has_any_preview:
                with Horizontal(id="form-split"):
                    with Vertical(id="form-left"):
                        yield from self._compose_body()
                    with VerticalScroll(id="form-preview-pane"):
                        yield Markdown("", id="form-preview")
            else:
                yield from self._compose_body()

    def _compose_body(self) -> ComposeResult:
        with FormBodyScroll(id="inline-form-body"):
            for index, question in enumerate(self.request.questions):
                with Vertical(id=f"options-{index}", classes="form-options"):
                    options = question.options or []
                    recommended = question.recommended or []
                    descriptions = question.descriptions or []
                    for option_index, (_value, label) in enumerate(options):
                        with Vertical(classes="form-option-row"):
                            with Horizontal(classes="form-option-line"):
                                yield Static(
                                    "",
                                    id=f"marker-{index}-{option_index}",
                                    classes="form-marker",
                                    markup=False,
                                )
                                yield self._content_widget(
                                    label,
                                    f"label-{index}-{option_index}",
                                    "form-label",
                                )
                                if option_index < len(recommended) and recommended[option_index]:
                                    yield Static(
                                        "(推荐)",
                                        id=f"recommended-{index}-{option_index}",
                                        classes="form-recommended",
                                        markup=False,
                                    )
                            if (
                                not question.has_previews
                                and option_index < len(descriptions)
                                and descriptions[option_index]
                            ):
                                yield self._content_widget(
                                    descriptions[option_index],
                                    f"description-{index}-{option_index}",
                                    "form-description",
                                )
                    with Horizontal(id=f"custom-row-{index}", classes="form-custom-row"):
                        yield Static("其它：", classes="form-input-prefix", markup=False)
                        yield AutoGrowTextArea(
                            "",
                            id=f"custom-input-{index}",
                            classes="form-input",
                            soft_wrap=True,
                            show_line_numbers=False,
                            placeholder="输入自定义回答…",
                        )
            with Horizontal(id="discussion-row", classes="form-discussion-row"):
                yield Static("讨论：", classes="form-input-prefix", markup=False)
                yield AutoGrowTextArea(
                    "",
                    id="discussion-input",
                    classes="form-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                    placeholder="讨论这几个问题…",
                )
            yield SelectionStatic("", id="form-summary", markup=False)

    def on_mount(self) -> None:
        questions = self.request.questions
        self._question_texts = [
            self.query_one(f"#question-text-{index}") for index in range(len(questions))
        ]
        self._option_panels = [
            self.query_one(f"#options-{index}") for index in range(len(questions))
        ]
        self._option_rows = [
            list(self._option_panels[index].query(".form-option-row"))
            for index in range(len(questions))
        ]
        self._markers = [
            [
                self.query_one(f"#marker-{index}-{option_index}", Static)
                for option_index in range(len(questions[index].options or []))
            ]
            for index in range(len(questions))
        ]
        self._custom_rows = [
            self.query_one(f"#custom-row-{index}") for index in range(len(questions))
        ]
        self._custom_inputs = [
            self.query_one(f"#custom-input-{index}", TextArea)
            for index in range(len(questions))
        ]
        self._discussion_row = self.query_one("#discussion-row")
        self._discussion_input = self.query_one("#discussion-input", TextArea)
        self._summary = self.query_one("#form-summary", SelectionStatic)
        self._render_form()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self._discussion_input:
            self.discussion = event.text_area.text
            return
        for index, custom_input in enumerate(self._custom_inputs):
            if event.text_area is not custom_input:
                continue
            self.custom[index] = event.text_area.text
            question = self.request.questions[index]
            if (
                question.options is not None
                and not question.multi_select
                and self.custom[index].strip()
                and self.checked[index]
            ):
                self.checked[index].clear()
                self._render_markers(index)
            self._render_tabs()
            return

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

    def _render_tabs(self) -> None:
        labels: list[str] = []
        for index, question in enumerate(self.request.questions):
            marker = "☑" if self._answer(index) else "☐"
            label = f"{marker} {question.header or f'问题{index + 1}'}"
            labels.append(f"[{label}]" if self.tab == index else label)
        labels.append("[提交]" if self.tab == len(self.request.questions) else "提交")
        self.query_one("#form-tabs", Static).update("  ".join(labels))

    def _render_markers(self, index: int) -> None:
        question = self.request.questions[index]
        options = question.options or []
        for option_index in range(len(options)):
            selected = option_index in self.checked[index]
            mark = (
                "[x]" if question.multi_select and selected
                else "[ ]" if question.multi_select
                else "●" if selected
                else "○"
            )
            cursor = (
                "❯" if self.zone == "answer" and self.rows[index] == option_index else " "
            )
            self._markers[index][option_index].update(f"{cursor} {mark} {option_index + 1}.")

    def _render_form(self) -> None:
        self._render_tabs()
        on_summary = self.tab == len(self.request.questions)
        for index in range(len(self.request.questions)):
            active = not on_summary and self.tab == index
            self._question_texts[index].display = active
            self._option_panels[index].display = active
            self._custom_rows[index].display = active and self.zone == "answer"
            if active:
                self._render_markers(index)
        self._summary.display = on_summary
        if on_summary:
            lines = ["回答小结"]
            for index in range(len(self.request.questions)):
                lines.append(f"问题{index + 1}：{self._answer(index) or '未作答'}")
            self._summary.update("\n".join(lines))
        self._discussion_row.display = self.zone == "discussion"

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

    def restore_focus(self) -> None:
        if self._completed:
            return
        target: Widget = self
        if self.zone == "discussion":
            target = self._discussion_input
        elif (
            self.tab < len(self.request.questions)
            and self.rows[self.tab]
            == self._option_count(self.request.questions[self.tab])
        ):
            target = self._custom_inputs[self.tab]
        self.screen.set_focus(target)
        if self.zone == "answer" and self.tab < len(self.request.questions):
            row = self.rows[self.tab]
            current = (
                self._custom_rows[self.tab]
                if row == self._option_count(self.request.questions[self.tab])
                else self._option_rows[self.tab][row]
            )
            current.scroll_visible(animate=False)

    def action_move_tab(self, delta: int) -> None:
        if self.zone != "answer":
            return
        self.tab = min(max(0, self.tab + delta), len(self.request.questions))
        self._render_form()

    def action_move_row(self, delta: int) -> None:
        if self.zone != "answer" or self.tab >= len(self.request.questions):
            return
        maximum = self._option_count(self.request.questions[self.tab])
        new_row = min(max(0, self.rows[self.tab] + delta), maximum)
        if new_row == self.rows[self.tab]:
            return
        self.rows[self.tab] = new_row
        self._render_form()

    def action_toggle(self) -> None:
        if self.zone == "discussion":
            self._discussion_input.insert(" ")
            return
        if self.tab >= len(self.request.questions):
            return
        question = self.request.questions[self.tab]
        options = question.options or []
        row = self.rows[self.tab]
        if row >= len(options):
            custom_input = self._custom_inputs[self.tab]
            if self.screen.focused is custom_input:
                custom_input.insert(" ")
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
            self._custom_inputs[self.tab].clear()
            # 单选选中后自动前进到下一个问题
            self.tab = min(self.tab + 1, len(self.request.questions))
        self._render_form()

    def action_choose_number(self, index: int) -> None:
        if self.zone == "discussion":
            self._discussion_input.insert(str(index))
            return
        if self.tab >= len(self.request.questions):
            return
        options = self.request.questions[self.tab].options or []
        custom_input = self._custom_inputs[self.tab]
        if self.rows[self.tab] == len(options) and self.screen.focused is custom_input:
            custom_input.insert(str(index))
            return
        if 1 <= index <= len(options):
            self.rows[self.tab] = index - 1
            self.action_toggle()

    def action_confirm(self) -> None:
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
        self.zone = "answer" if self.zone == "discussion" else "discussion"
        self._render_form()

    def action_newline(self) -> None:
        if self.zone == "discussion":
            target = self._discussion_input
        elif self.tab < len(self.request.questions):
            target = self._custom_inputs[self.tab]
        else:
            return
        if self.screen.focused is target:
            target.insert("\n")

    def _submit(self) -> None:
        if self._completed:
            return
        payload = json.dumps(
            {
                "answers": [self._answer(index) for index in range(len(self.request.questions))],
                "discussion": self.discussion.strip(),
            },
            ensure_ascii=False,
        )
        self._finish(DialogResult(value=payload))

    def action_cancel(self) -> None:
        self._cancel()

    def build_summary(self, result: DialogResult) -> str:
        """构建提交后的回答摘要文本。"""
        if result.cancelled:
            return "[用户取消了作答]"
        parts: list[str] = []
        for index, question in enumerate(self.request.questions):
            header = question.header or f"问题{index + 1}"
            answer = self._answer(index) or "未作答"
            parts.append(f"  {header} → {answer}")
        lines = ["用户选择："] + parts
        if self.discussion.strip():
            lines.append(f"讨论：{self.discussion.strip()}")
        return "\n".join(lines)


def make_inline_widget(request: MenuRequest, task: str = "") -> InlineWidget:
    if isinstance(request, FormMenu):
        return InlineFormWidget(request, task=task)
    if isinstance(request, ModelMenu):
        return InlineModelSelectionWidget(request)
    if isinstance(request, ChoiceInputMenu):
        return InlineChoiceInputWidget(request)
    if isinstance(request, (PermissionMenu, ChoiceMenu)):
        return InlineSelectionWidget(request)
    raise TypeError(f"unsupported inline request: {type(request)!r}")
