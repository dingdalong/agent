"""Non-TTY frontend with guaranteed ANSI-free writes."""

from __future__ import annotations

import json
import re
import sys
from typing import TextIO

from prompt_toolkit import PromptSession
from rich.text import Text

from src.events.menu import FormQuestion


_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class PlainFrontend:
    """Write plain text to a stream without terminal control sequences."""

    def __init__(self, stream: TextIO | None = None) -> None:
        """Initialize a plain frontend.

        Args:
            stream: Destination stream; None follows the current ``sys.stdout``.

        Returns:
            None.
        """
        self._stream = stream

    def write(self, content: str | Text, end: str = "") -> None:
        """Write styled or raw content as ANSI-free text.

        Args:
            content: Rich Text or string written to the destination.
            end: Suffix appended after content.

        Returns:
            None.
        """
        value = content.plain if isinstance(content, Text) else content
        stream = self._stream or sys.stdout
        stream.write(_ANSI_SEQUENCE.sub("", value) + end)
        stream.flush()


class PlainActions:
    """Implement all non-TTY input and menu workflows."""

    async def _read_input_plain(self, prompt: str, default: str) -> str:
        """非 TTY 降级输入：用单个 PromptSession.prompt_async 读取（无状态条、无常驻 App）。

        Args:
            prompt: 提示文本。
            default: 预填默认值。
        Returns:
            用户输入文本。
        """
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        return await self._fallback_session.prompt_async(
            prompt, default=default, multiline=True, prompt_continuation="... ", handle_sigint=True
        )

    async def _read_permission_plain(self, tool_name: str, detail: str) -> str:
        """非 TTY 降级权限确认：打印说明块后读取 y/n。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
        Returns:
            "yes" 或 "deny"。
        """
        self._print_rich(self._permission_prompt_text(tool_name, detail), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        hint = "请输入 y 或 n。"
        while True:
            answer = (await self._fallback_session.prompt_async("选择: ", handle_sigint=True)).strip().lower()
            decision = self._normalize_permission_answer(answer)
            if decision is not None:
                return decision
            self._print_rich(hint, style="red")

    def _plain_choice_menu(self, options: list[tuple[str, str]], descriptions: list[str] | None) -> Text:
        """构建非 TTY 降级选择菜单文本：逐项「[编号] 标签」，有参考说明者紧随浅色缩进副行。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的参考说明（空串/越界视为无）；None 表示无任何说明。
        Returns:
            菜单 Text（多行，含结尾换行）。
        """
        menu = Text()
        for i, (_value, label) in enumerate(options):
            menu.append(f"  [{i + 1}] {label}\n")
            desc = descriptions[i].strip() if descriptions and i < len(descriptions) else ""
            if desc:
                menu.append(f"      {desc}\n", style="bright_black")
        return menu

    async def _read_choice_plain(self, prompt: str, options: list[tuple[str, str]], default_index: int,
                                 descriptions: list[str] | None = None) -> str:
        """非 TTY 降级选择：打印编号菜单后用 PromptSession 读数字，映射回 value。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标（降级路径仅用于回车空输入时的默认值）。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
        Returns:
            所选项的 value；空输入返回默认项 value，无默认时返回空串（取消）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        while True:
            answer = (await self._fallback_session.prompt_async(f"选择 (1-{len(options)}): ", handle_sigint=True)).strip()
            if not answer:
                return options[default_index][0] if 0 <= default_index < len(options) else ""
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    return options[idx][0]
            self._print_rich("请输入有效编号。", style="red")

    async def _read_choice_input_plain(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
    ) -> str:
        """非 TTY 降级 choice_input：打印编号菜单后读一行；纯合法编号返回该项 choice，
        否则整行作输入 text；空输入返回默认项 choice、无默认项则取消。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
            input_placeholder: 输入行占位文案（并入读取提示）。
            default_index: 空输入时回退的默认项下标。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；无有效输入且无默认项时为空串（取消）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        hint = input_placeholder.strip() or "输入回答"
        answer = (await self._fallback_session.prompt_async(
            f"选择编号 1-{len(options)} 或直接{hint}: ", handle_sigint=True
        )).strip()
        if not answer:
            if 0 <= default_index < len(options):
                return json.dumps({"choice": options[default_index][0], "text": ""})
            return ""
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(options):
                return json.dumps({"choice": options[idx][0], "text": ""})
        return json.dumps({"choice": "", "text": answer})

    async def _read_form_plain(self, prompt: str, questions: list[FormQuestion]) -> str:
        """非 TTY 降级表单：打印上文后逐题采集（多选读编号集、单选读编号、自由文本读整行），末尾收讨论，JSON 编码返回。

        Args:
            prompt: 表单上文提示。
            questions: 问题列表。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串（answers 与 questions 顺序对齐）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        answers: list[str] = []
        for qi, question in enumerate(questions):
            self._print_rich(f"\n问题{qi + 1}：{question.question}")
            if question.options is None:
                answers.append(await self._read_input_plain("你的回答: ", ""))
            elif question.multi_select:
                answers.append(await self._read_multi_choice_plain(question.options, question.descriptions))
            else:
                answers.append(await self._read_choice_plain("", question.options, 0, question.descriptions))
        discussion = await self._read_input_plain("讨论这几个问题（可回车跳过）: ", "")
        return json.dumps({"answers": answers, "discussion": discussion})

    async def _read_multi_choice_plain(self, options: list[tuple[str, str]],
                                       descriptions: list[str] | None = None) -> str:
        """非 TTY 降级多选：打印编号菜单后读一行；全为合法编号则按「、」连接对应 value，否则整行作自定义文本。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
        Returns:
            选中项 value 的「、」连接串（去重保序）；输入含非编号内容时原样作自定义文本返回；空输入返回空串。
        """
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        answer = (await self._fallback_session.prompt_async(
            "多选（逗号分隔编号，或直接输入自定义回答，回车跳过）: ", handle_sigint=True
        )).strip()
        if not answer:
            return ""
        tokens = [t.strip() for t in answer.replace("，", ",").split(",") if t.strip()]
        if tokens and all(t.isdigit() and 1 <= int(t) <= len(options) for t in tokens):
            seen: set[int] = set()
            values: list[str] = []
            for token in tokens:
                idx = int(token) - 1
                if idx not in seen:
                    seen.add(idx)
                    values.append(options[idx][0])
            return "、".join(values)
        return answer  # 含非编号内容：整行作自定义文本
