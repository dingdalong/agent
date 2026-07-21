"""Multi-question form state controller."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from rich.text import Text

from src.events.menu import FormQuestion


@dataclass(slots=True)
class FormState:
    """Mutable state for one single-screen multi-question form."""

    questions: list[FormQuestion] | None = None
    focus: int = 0
    zone: str = "answer"
    rows: list[int] = field(default_factory=list)
    checked: list[set[int]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    discussion: str = ""
    markdown: bool = False


def _fit_display_width(text: str, max_cols: int) -> str:
    """Truncate text to a terminal display width.

    Args:
        text: Source label.
        max_cols: Maximum terminal columns.

    Returns:
        Prefix whose East Asian display width fits the limit.
    """
    columns = 0
    output: list[str] = []
    for character in text:
        width = 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        if columns + width > max_cols:
            break
        output.append(character)
        columns += width
    return "".join(output)


class FormActions:
    """Render and operate multi-question form flows."""

    def _render_form(self) -> ANSI:
        """渲染单屏标签页表单：顶部标签栏 + 聚焦标签内容 + 操作提示，供 form_window 使用。

        标签栏逐题「问题N」加末尾「提交」标签，聚焦项反显；问题标签展示题干与答案行（单选 ●/○ 单选圈+编号、
        多选 [x]/[ ] 复选框，末行恒有自定义输入行），光标行反显；「提交」标签展示逐题作答小结。
        底部讨论栏由常驻输入框承载，不在此渲染。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃表单时为空。
        """
        if not self._form_questions:
            return ANSI("")
        text = Text("\n")
        text.append_text(self._render_form_tabs())
        text.append("\n\n")
        if self._form_on_submit_tab():
            text.append_text(self._render_form_submit_page())
        else:
            text.append_text(self._render_form_question_page())
        text.append("\n")
        text.append_text(self._render_form_hint())
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_form_tabs(self) -> Text:
        """渲染顶部标签栏：逐题「答题状态 + header 简介」（已答 ☑ 未答 ☐；header 截断至 12 列，空则回退「问题N」）+ 末尾「提交」，聚焦标签反显（问题青、提交绿），余压暗。

        Returns:
            标签栏 Text（单行）。
        """
        tabs = Text()
        for qi in range(len(self._form_questions)):
            header = self._form_questions[qi].header
            label_text = _fit_display_width(header, 12) if header else f"问题{qi + 1}"
            answered = bool(self._collect_form_answer(qi, self._form_questions[qi]).strip())
            marker = "☑" if answered else "☐"
            label = Text(f" {marker} {label_text} ")
            label.stylize("reverse cyan" if qi == self._form_focus else "bright_black")
            tabs.append_text(label)
            tabs.append(" ")
        submit = Text(" 提交 ")
        submit.stylize("reverse green" if self._form_on_submit_tab() else "bright_black")
        tabs.append_text(submit)
        return tabs

    def _render_form_question_page(self) -> Text:
        """渲染聚焦问题标签页：题干（加粗）+ 各答案行（选项行，含参考说明的紧随浅色缩进副行 + 自定义输入行），光标行反显。

        Returns:
            问题标签页内容 Text（多行）。
        """
        qi = self._form_focus
        question = self._form_questions[qi]
        multi = question.options is not None and question.multi_select
        cursor = self._form_row[qi]
        body = Text()
        title = self._markdown_label(question.question) if self._form_markdown else Text(question.question)
        title.stylize("bold")  # 题干加粗（Markdown 分支叠加在既有样式之上）
        body.append_text(title)
        body.append("\n")
        if question.options is not None:
            descs = question.descriptions or []
            for oi, (_value, label) in enumerate(question.options):
                body.append_text(self._render_form_option_line(qi, oi, label, multi, cursor == oi))
                body.append("\n")
                desc = descs[oi].strip() if oi < len(descs) else ""
                if desc:  # 选项参考说明：浅色缩进副行，不参与光标反显
                    sub = self._markdown_label(desc) if self._form_markdown else Text(desc)
                    sub.stylize("bright_black")
                    body.append("     ")
                    body.append_text(sub)
                    body.append("\n")
        body.append_text(self._render_form_custom_line(qi, multi, cursor == self._form_option_count(qi)))
        return body

    def _render_form_option_line(self, qi: int, oi: int, label: str, multi: bool, on_cursor: bool) -> Text:
        """渲染一个选项行；多选为 [x]/[ ] 复选框+标签，单选为 ●/○ 单选圈+编号+标签，选中项标记染绿，光标所在行整行反显。

        Args:
            qi: 问题下标。
            oi: 选项下标。
            label: 选项标签文本。
            multi: 该题是否多选。
            on_cursor: 光标是否落在本行。
        Returns:
            选项行 Text（单行）。
        """
        line = Text(" ")
        selected = oi in self._form_checked[qi]
        if multi:
            line.append("[x] " if selected else "[ ] ", style="green" if selected else "")
            line.append_text(self._markdown_label(label) if self._form_markdown else Text(label))
        else:
            line.append("● " if selected else "○ ", style="green" if selected else "")
            line.append(f"{oi + 1}. ")
            line.append_text(self._markdown_label(label) if self._form_markdown else Text(label))
        if on_cursor:
            line.stylize("reverse")
        return line

    def _render_form_custom_line(self, qi: int, multi: bool, on_cursor: bool) -> Text:
        """渲染自定义输入行；答题区光标在此且缓冲可编辑时取输入缓冲实时文本、在插入点内联绘制块状光标（不整行反显），否则取暂存文本、空则浅字占位并整行反显。
        前缀标记随「是否已填」染绿：多选 [x]/[ ] 复选框，单选 ●/○ 单选圈（自定义文本即单选的一项）。

        Args:
            qi: 问题下标。
            multi: 该题是否多选（多选前缀复选框，单选前缀单选圈）。
            on_cursor: 光标是否落在本行。
        Returns:
            自定义输入行 Text（单行）。
        """
        editing = on_cursor and self._form_zone == "answer" and self._buffer is not None and self._buffer_editable()
        value = self._buffer.text if editing else self._form_text[qi]
        line = Text(" ")
        filled = bool(value.strip())
        if multi:
            line.append("[x] " if filled else "[ ] ", style="green" if filled else "")
        else:
            line.append("● " if filled else "○ ", style="green" if filled else "")
        line.append("⌨ 其他: ", style="cyan" if on_cursor else "bright_black")
        if editing:  # 内联绘制块状光标：光标块为唯一反显，空缓冲则块光标 + 浅字占位；不整行反显
            self._append_caret_text(line, value, self._buffer.cursor_position)
            if not value:
                line.append("输入回答…", style="bright_black")
            return line
        if value:
            line.append(value)
        else:
            line.append("输入回答…", style="bright_black")
        if on_cursor:
            line.stylize("reverse")
        return line

    def _append_caret_text(self, line: Text, text: str, pos: int) -> None:
        """把文本连同插入点块状光标就地追加到行：插入点处字符反显作块光标；插入点在行尾或文本为空时以反显空格作块光标。

        Args:
            line: 目标 Text（就地追加，不返回）。
            text: 输入缓冲文本。
            pos: 插入点（光标）在文本中的下标。
        """
        pos = max(0, min(pos, len(text)))
        line.append(text[:pos])
        if pos < len(text):
            line.append(text[pos], style="reverse")
            line.append(text[pos + 1:])
        else:
            line.append(" ", style="reverse")

    def _render_form_submit_page(self) -> Text:
        """渲染「提交」标签页：逐题作答小结（已答显示答案，未答标注「未作答」）。

        Returns:
            提交标签页内容 Text（多行）。
        """
        body = Text()
        body.append("回答小结\n", style="bold green")
        for qi, question in enumerate(self._form_questions):
            answer = self._collect_form_answer(qi, question).strip()
            line = Text(" ")
            line.append(f"问题{qi + 1}：", style="cyan")
            if answer:
                line.append(answer)
            else:
                line.append("未作答", style="bright_black")
            body.append_text(line)
            body.append("\n")
        return body

    def _render_form_hint(self) -> Text:
        """渲染底部操作提示行，按焦点区与标签类型给出可用按键。

        Returns:
            操作提示 Text（单行）。
        """
        if self._form_zone == "discuss":
            return Text("Tab 返回答题 · Enter 提交 · Esc 取消", style="bright_black")
        if self._form_on_submit_tab():
            return Text("Enter 提交 · ←→ 返回修改 · Esc 取消", style="bright_black")
        on_custom = self._form_cursor_on_custom()
        keys = ["↑↓ 选择行"]
        if not on_custom and self._form_focused_multi():
            keys.append("空格 勾选")
        elif not on_custom and self._form_focused_single():
            keys.append("Enter/空格 选中")
        else:
            keys.append("Enter 确认")
        keys += ["←→ 切换标签", "Esc 取消"]
        return Text(" · ".join(keys), style="bright_black")

    def _render_form_footer(self) -> ANSI:
        """渲染表单答题区底部提示行（输入栏隐藏时占其位）：暗色提示按 Tab 唤出讨论输入栏，文案与输入栏占位一致。

        Returns:
            可作为 Window 内容的 ANSI（单行暗色提示）。
        """
        with self._status_console.capture() as capture:
            self._status_console.print(Text("Tab 讨论这几个问题…", style="bright_black"), end="")
        return ANSI(capture.get())

    def _current_form_question(self) -> FormQuestion | None:
        """返回当前聚焦的表单问题；无活跃表单、聚焦「提交」标签或下标越界时返回 None。

        Returns:
            当前聚焦的 FormQuestion，或 None。
        """
        if not self._form_questions or not (0 <= self._form_focus < len(self._form_questions)):
            return None
        return self._form_questions[self._form_focus]

    def _form_on_submit_tab(self) -> bool:
        """当前是否聚焦「提交」标签（焦点下标等于问题数）。

        Returns:
            聚焦「提交」标签时为 True。
        """
        return bool(self._form_questions) and self._form_focus == len(self._form_questions)

    def _form_option_count(self, qi: int) -> int:
        """第 qi 题的选项数（自由文本题为 0）。

        Args:
            qi: 问题下标。
        Returns:
            该题选项数量。
        """
        question = self._form_questions[qi]
        return len(question.options) if question.options else 0

    def _form_cursor_on_custom(self) -> bool:
        """答题区光标当前是否落在聚焦问题的自定义输入行（提交标签或无表单时恒 False）。

        Returns:
            光标在自定义输入行时为 True。
        """
        if self._current_form_question() is None:
            return False
        return self._form_row[self._form_focus] >= self._form_option_count(self._form_focus)

    def _form_focused_multi(self) -> bool:
        """聚焦问题是否为多选题（有 options 且 multi_select）。

        Returns:
            聚焦问题为多选题时为 True。
        """
        question = self._current_form_question()
        return question is not None and question.options is not None and question.multi_select

    def _form_focused_single(self) -> bool:
        """聚焦问题是否为单选题（有 options 且非 multi_select）。

        Returns:
            聚焦问题为单选题时为 True。
        """
        question = self._current_form_question()
        return question is not None and question.options is not None and not question.multi_select

    def _form_answering(self) -> bool:
        """是否处于表单答题区（form 态且焦点在 answer 区，即输入栏隐藏、底部提示显示之态）。

        Returns:
            form 态且 _form_zone == "answer" 时为 True。
        """
        return self._mode == "form" and self._form_zone == "answer"

    async def _await_form(self, questions: list[FormQuestion], markdown: bool) -> str:
        """进入单屏表单态（form），await 由表单键位（Enter 提交 / Esc 取消）解析的 future。

        初始化各题光标/勾选/文本与讨论状态并聚焦首标签；若焦点在 agent 列表先抢回输入框（否则按键落到列表而非应答 future）。

        Args:
            questions: 问题列表。
            markdown: 问题/选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串；Esc 取消时为空串。
        """
        with self._human_interaction() as future:
            try:
                self._form_questions = questions
                self._form_row = [0] * len(questions)
                self._form_checked = [set() for _ in questions]
                self._form_text = [""] * len(questions)
                self._form_discussion = ""
                self._form_focus = 0
                self._form_zone = "answer"
                self._form_markdown = markdown
                self._mode = "form"
                if (
                    self._app is not None
                    and self._agent_list_inner is not None
                    and self._app.layout.has_focus(self._agent_list_inner)
                ):
                    self._app.layout.focus(self._input_window)
                self._form_load_buffer()
                self._app.invalidate()
                return await future
            finally:
                self._form_questions = None
                self._enter_processing_idle()

    def _form_commit_buffer(self) -> None:
        """把输入框当前文本提交回当前 sink：答题区光标在自定义行→该题自定义文本（单选题填入非空文本时
        清除其已选选项，保持单选互斥），否则→讨论栏。"""
        if self._buffer is None:
            return
        if self._form_zone == "answer" and self._form_cursor_on_custom():
            qi = self._form_focus
            self._form_text[qi] = self._buffer.text
            question = self._form_questions[qi]
            if question.options is not None and not question.multi_select and self._buffer.text.strip():
                self._form_checked[qi].clear()
        else:
            self._form_discussion = self._buffer.text

    def _form_load_buffer(self) -> None:
        """把当前 sink 文本载入输入框：答题区光标在自定义行→载该题自定义文本，否则→载讨论栏文本。"""
        if self._buffer is None:
            return
        if self._form_zone == "answer" and self._form_cursor_on_custom():
            value = self._form_text[self._form_focus]
        else:
            value = self._form_discussion
        self._buffer.set_document(Document(value, len(value)), bypass_readonly=True)

    def _move_form_focus(self, delta: int) -> None:
        """在标签间移动焦点（含末尾「提交」标签），到边界夹取、不循环。

        Args:
            delta: 移动步长（+1 下一标签、-1 上一标签）。
        """
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_focus = max(0, min(self._form_focus + delta, len(self._form_questions)))
        self._form_load_buffer()

    def _move_form_row(self, delta: int) -> None:
        """在聚焦问题的答案行间移动光标（选项行 + 自定义输入行），到边界夹取；提交标签无操作。

        Args:
            delta: 移动步长（+1 下一行、-1 上一行）。
        """
        if self._current_form_question() is None:
            return
        self._form_commit_buffer()
        last = self._form_option_count(self._form_focus)  # 自定义行下标 == 选项数
        self._form_row[self._form_focus] = max(0, min(self._form_row[self._form_focus] + delta, last))
        self._form_load_buffer()

    def _toggle_form_zone(self) -> None:
        """在答题区与底部讨论栏之间切换焦点（切换前后同步输入框 sink）。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_zone = "discuss" if self._form_zone == "answer" else "answer"
        self._form_load_buffer()

    def _toggle_form_option(self) -> None:
        """翻转聚焦问题当前光标所在选项的选中态：多选题增删该项；单选题为单选切换——选中它并清除其余选项，
        再次切换同一已选项则取消（回到未作答），选中时同步清空该题自定义文本以保持单选互斥。
        无选项题或光标在自定义行时无操作。"""
        question = self._current_form_question()
        if question is None or question.options is None or self._form_cursor_on_custom():
            return
        qi = self._form_focus
        row = self._form_row[qi]
        selected = self._form_checked[qi]
        if question.multi_select:
            if row in selected:
                selected.discard(row)
            else:
                selected.add(row)
        elif row in selected:
            selected.clear()
        else:
            selected.clear()
            selected.add(row)
            self._form_text[qi] = ""

    def _form_number(self, index: int) -> None:
        """数字键操作聚焦问题的第 index 选项并把光标跳到该行：多选题翻转其勾选；单选题直接选中它
        （替换其余选项并清空该题自定义文本，保持单选互斥）；越界忽略。

        Args:
            index: 选项下标（0 基）。
        """
        question = self._current_form_question()
        if question is None or question.options is None or not (0 <= index < len(question.options)):
            return
        qi = self._form_focus
        selected = self._form_checked[qi]
        if question.multi_select:
            if index in selected:
                selected.discard(index)
            else:
                selected.add(index)
        else:
            selected.clear()
            selected.add(index)
            self._form_text[qi] = ""
        self._form_row[qi] = index

    def _confirm_question(self) -> None:
        """确认当前问题（暂存自定义文本）并自动切到下一标签；末题后落到「提交」标签。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_focus = min(self._form_focus + 1, len(self._form_questions))
        self._form_load_buffer()

    def _collect_form_answer(self, qi: int, question: FormQuestion) -> str:
        """汇总第 qi 题的最终答案字符串。

        Args:
            qi: 问题下标。
            question: 对应的 FormQuestion。
        Returns:
            自由文本题为其自定义文本；单选题取选中项 value（未选中任何选项则取自定义文本，皆无为空串=未作答）；
            多选题为全部勾选项 value 与非空自定义文本以「、」连接。
        """
        if question.options is None:
            return self._form_text[qi]
        if question.multi_select:
            parts = [question.options[i][0] for i in sorted(self._form_checked[qi]) if i < len(question.options)]
            if self._form_text[qi].strip():
                parts.append(self._form_text[qi].strip())
            return "、".join(parts)
        if self._form_checked[qi]:  # 单选：已选选项优先
            oi = min(self._form_checked[qi])
            if oi < len(question.options):
                return question.options[oi][0]
        return self._form_text[qi]  # 未选中任何选项：取自定义文本（空串=未作答）

    def _submit_form(self) -> None:
        """收集全部作答与讨论文本（先提交当前输入框），以 JSON({answers,discussion}) 解析表单 future。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        answers = [self._collect_form_answer(qi, question) for qi, question in enumerate(self._form_questions)]
        self._resolve_input(json.dumps({"answers": answers, "discussion": self._form_discussion}))

    def _cancel_form(self) -> None:
        """取消表单：以空串解析 future（上层 request_form 据此返回 ([], "") 表示取消）。"""
        self._resolve_input("")

    async def _read_form(self, prompt: str, questions: list[FormQuestion], markdown: bool = False) -> str:
        """以单屏表单读取多个问题的作答（ask_user 多问题）。

        非 TTY 走扁平降级（逐题打印后顺序读取）。

        Args:
            prompt: 表单上文提示。
            questions: 问题列表，每项带可选 (value, label) 选项（无则自由文本）。
            markdown: 上文提示与问题/选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串（answers 与 questions 顺序对齐）；空串表示取消。
        """
        if not questions:
            return ""
        if not self._tty:
            return await self._read_form_plain(prompt, questions)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_form(questions, markdown)
