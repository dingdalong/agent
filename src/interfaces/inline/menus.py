"""Selection and choice-input state controllers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from rich.text import Text


@dataclass(slots=True)
class SelectionState:
    """Mutable direction-key selection menu state."""

    options: list[tuple[str, str]] | None = None
    index: int = 0
    cancel_value: str = ""
    markdown: bool = False


@dataclass(slots=True)
class ChoiceInputState:
    """Mutable options-plus-free-text interaction state."""

    options: list[tuple[str, str]] | None = None
    descriptions: list[str] | None = None
    placeholder: str = ""
    index: int = 0
    markdown: bool = False


class MenuActions:
    """Render and operate selection, permission, and ChoiceInput flows."""

    def _render_select(self) -> ANSI:
        """渲染选择菜单选项列表（每选项一行），选中行以 ❯ + 反显标记；供 select_window 使用。

        每行格式：「<marker><序号>. <label>」，序号从 1 起，便于数字键直选。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃菜单时为空。
        """
        if not self._select_options:
            return ANSI("")
        text = Text()
        for i, (_value, label) in enumerate(self._select_options):
            selected = i == self._select_index
            line = Text()
            line.append("❯ " if selected else "  ", style="cyan" if selected else "")
            line.append(f"{i + 1}. ")
            line.append_text(self._markdown_label(label) if self._select_markdown else Text(label))
            if selected:
                line.stylize("reverse")  # 整行反显（叠加在标签自带的 Markdown 样式之上）
            text.append(line)
            if i < len(self._select_options) - 1:
                text.append("\n")
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_choice_input(self) -> ANSI:
        """渲染 choice_input 菜单的选项区与操作提示（输入行由下方常驻输入框承载，不在此渲染）。

        逐选项一行「<marker><序号>. <label>」，光标落在某选项行时该行 ❯ 前缀 + 整行反显；可带浅色说明副行。
        光标在输入行时所有选项行均无 ❯（改由下方输入框醒目前缀提示）。末行操作提示随光标位置给出可用按键。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃菜单时为空。
        """
        if not self._choice_input_options:
            return ANSI("")
        descs = self._choice_input_descriptions or []
        on_input = self._choice_input_on_input_row()
        text = Text()
        for i, (_value, label) in enumerate(self._choice_input_options):
            selected = (not on_input) and i == self._choice_input_index
            line = Text()
            line.append("❯ " if selected else "  ", style="cyan" if selected else "")
            line.append(f"{i + 1}. ")
            line.append_text(self._markdown_label(label) if self._choice_input_markdown else Text(label))
            if selected:
                line.stylize("reverse")
            text.append_text(line)
            text.append("\n")
            desc = descs[i].strip() if i < len(descs) else ""
            if desc:  # 选项参考说明：浅色缩进副行，不参与光标反显
                sub = self._markdown_label(desc) if self._choice_input_markdown else Text(desc)
                sub.stylize("bright_black")
                text.append("     ")
                text.append_text(sub)
                text.append("\n")
        text.append_text(self._render_choice_input_hint(on_input))
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_choice_input_hint(self, on_input_row: bool) -> Text:
        """渲染 choice_input 底部操作提示行，按光标是否在输入行给出可用按键。

        Args:
            on_input_row: 光标是否落在末行输入行。
        Returns:
            操作提示 Text（单行，浅色）。
        """
        if on_input_row:
            return Text("↑↓ 选择行 · Enter 提交输入 · Esc 取消", style="bright_black")
        return Text("↑↓ 选择行 · Enter/数字 选项 · Esc 取消", style="bright_black")

    async def _await_selection(
        self, options: list[tuple[str, str]], default_index: int, cancel_value: str, markdown: bool = False
    ) -> str:
        """进入只读选择菜单（select 态），await 由方向键/数字/Enter/Esc 绑定解析的 future。

        若当前焦点在 agent 列表，先抢回输入框（否则按键落到列表而非应答 future）。

        Args:
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标（夹取到合法范围）。
            cancel_value: Esc 取消时解析返回的 value。
            markdown: 选项标签是否按 Markdown 渲染。
        Returns:
            所选项的 value，或 Esc 取消时的 cancel_value。
        """
        with self._human_interaction() as future:
            try:
                self._select_options = options
                self._select_index = max(0, min(default_index, len(options) - 1)) if options else 0
                self._select_cancel_value = cancel_value
                self._select_markdown = markdown
                if (
                    self._app is not None
                    and self._agent_list_inner is not None
                    and self._app.layout.has_focus(self._agent_list_inner)
                ):
                    self._app.layout.focus(self._input_window)
                self._app.invalidate()
                return await future
            finally:
                self._select_options = None
                self._enter_processing_idle()

    def _choice_input_on_input_row(self) -> bool:
        """choice_input 态光标当前是否落在末行输入行（非 choice_input 态或无活跃菜单时恒 False）。

        Returns:
            光标在输入行（下标 == 选项数）时为 True。
        """
        if self._choice_input_options is None:
            return False
        return self._choice_input_index >= len(self._choice_input_options)

    async def _await_choice_input(
        self,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool,
    ) -> str:
        """进入 choice_input 态，await 由 choice_input 键位（选项/输入行 Enter、数字、Esc）解析的 future。

        初始化选项/说明/占位/光标行与 Markdown 标志并清空输入缓冲；若焦点在 agent 列表先抢回输入框
        （否则按键落到列表而非应答 future）。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标（夹取到选项范围）。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；Esc 取消时为空串。
        """
        with self._human_interaction() as future:
            try:
                self._choice_input_options = options
                self._choice_input_descriptions = descriptions
                self._choice_input_placeholder = input_placeholder
                self._choice_input_index = max(0, min(default_index, len(options) - 1)) if options else 0
                self._choice_input_markdown = markdown
                if (
                    self._app is not None
                    and self._agent_list_inner is not None
                    and self._app.layout.has_focus(self._agent_list_inner)
                ):
                    self._app.layout.focus(self._input_window)
                self._buffer.set_document(Document("", 0), bypass_readonly=True)
                self._app.invalidate()
                return await future
            finally:
                self._choice_input_options = None
                self._enter_processing_idle()

    def _move_choice_input_row(self, delta: int) -> None:
        """在选项行与输入行之间移动光标（0..选项数），到边界夹取、不循环。

        跨越输入行会改变缓冲可编辑性与真实光标显隐，重绘由常驻 App 刷新完成。

        Args:
            delta: 移动步长（+1 下一行、-1 上一行）。
        """
        if not self._choice_input_options:
            return
        last = len(self._choice_input_options)  # 输入行下标 == 选项数
        self._choice_input_index = max(0, min(self._choice_input_index + delta, last))

    def _submit_choice_input_option(self) -> None:
        """以当前光标所在选项的 value 解析 choice_input future（text 为空）；光标不在选项行时无操作。"""
        if not self._choice_input_options or self._choice_input_on_input_row():
            return
        value = self._choice_input_options[self._choice_input_index][0]
        self._resolve_input(json.dumps({"choice": value, "text": ""}))

    def _submit_choice_input_text(self) -> None:
        """以输入行文本解析 choice_input future（choice 为空）；文本为空白则不提交（留在输入行）。"""
        if self._buffer is None:
            return
        text = self._buffer.text.strip()
        if not text:
            return
        self._resolve_input(json.dumps({"choice": "", "text": text}))

    def _choice_input_number(self, index: int) -> None:
        """数字键直选第 index 选项并立即提交（choice=该项 value、text 为空）；越界忽略。

        Args:
            index: 选项下标（0 基）。
        """
        if not self._choice_input_options or not (0 <= index < len(self._choice_input_options)):
            return
        value = self._choice_input_options[index][0]
        self._resolve_input(json.dumps({"choice": value, "text": ""}))

    def _cancel_choice_input(self) -> None:
        """取消 choice_input：以空串解析 future（上层 request_choice_input 据此返回 ("", "") 表示取消）。"""
        self._resolve_input("")

    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
    ) -> str:
        """工具权限确认：打印权限说明上文到 App 上方，再以方向键选择菜单读取决策。

        非 TTY 走扁平降级（打字 y/n）。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
        Returns:
            "yes" 或 "deny"。
        """
        if not self._tty:
            return await self._read_permission_plain(tool_name, detail)
        self._print_rich(self._permission_context_text(tool_name, detail), end="")
        return await self._await_selection(self._permission_options(), 0, cancel_value="deny")

    async def _read_choice(
        self, prompt: str, options: list[tuple[str, str]], default_index: int, markdown: bool = False
    ) -> str:
        """以方向键选择菜单读取一次选择。

        非 TTY 走扁平降级（打印编号菜单 + 读数字，纯文本）。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            所选项的 value；空串表示取消（Esc）。
        """
        if not self._tty:
            return await self._read_choice_plain(prompt, options, default_index)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_selection(options, default_index, cancel_value="", markdown=markdown)

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """以「选项列表 + 输入行」读取一次作答（choice_input，如 exit_plan_mode）。

        非 TTY 走扁平降级（编号菜单 + 读一行：合法编号返回该项，否则整行作输入文本）。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；空串表示取消。
        """
        if not self._tty:
            return await self._read_choice_input_plain(prompt, options, descriptions, input_placeholder, default_index)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_choice_input(options, descriptions, input_placeholder, default_index, markdown)

    def _permission_options(self) -> list[tuple[str, str]]:
        """构建一次性 yes/no 权限菜单。"""
        return [("yes", "允许一次"), ("deny", "拒绝 (esc)")]

    def _permission_context_text(self, tool_name: str, detail: str) -> Text:
        """构建权限确认上文。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
        Returns:
            可经 _print_rich 输出的 Rich Text。
        """
        prompt_text = Text()
        prompt_text.append("\n")
        prompt_text.append("工具请求权限", style="yellow")
        prompt_text.append(f"\n  工具: {tool_name}\n")
        prompt_text.append(f"  内容: {detail}\n")
        return prompt_text

    def _permission_prompt_text(self, tool_name: str, detail: str) -> Text:
        """构建非 TTY 权限确认说明块。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
        Returns:
            可经 _print_rich 输出的 Rich Text。
        """
        prompt_text = self._permission_context_text(tool_name, detail)
        prompt_text.append("  输入 y/n 后按 Enter 确认\n")
        prompt_text.append("  [y] 允许一次   [n] 拒绝\n")
        return prompt_text

    def _normalize_permission_answer(self, answer: str) -> str | None:
        """把用户输入归一化为权限决策；非法返回 None。

        Args:
            answer: 已 strip/lower 的用户输入。
        Returns:
            "yes"/"deny"，非法时 None。
        """
        if answer in {"y", "yes"}:
            return "yes"
        if answer in {"n", "no", "deny"}:
            return "deny"
        return None
