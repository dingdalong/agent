"""非 TTY 与启动确认使用的无 ANSI 纯文本前端。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

from rich.text import Text

from src.events.menu import FormQuestion
from src.tools.display import tool_title

try:  # POSIX 专有；Windows 上缺失时降级为不做终端规范化
    import termios
except ImportError:  # pragma: no cover - 非 POSIX 平台
    termios = None  # type: ignore[assignment]


LineReader = Callable[[str], Awaitable[str]]


def normalize_line_input(stream: TextIO | None = None) -> list | None:
    """把终端恢复成 canonical 行输入，返回原属性供还原；不适用时返回 None。

    Textual 进入 raw 模式时会清除 ICRNL/ICANON/ECHO（见其 driver 的
    `_patch_iflag`/`_patch_lflag`）。异常退出未走完恢复流程时，这些位会残留在
    终端上：回车发出的 CR 不再翻译成 NL，而 canonical 模式只认 NL/EOL/EOF 作
    行分隔符，于是 readline() 永远等不到行结束符而卡死，ECHO 还把 CR 回显成
    字面量 ^M。这里只按位补齐必需的几位，不整体覆写，避免破坏用户的其他设置。

    Args:
        stream: 目标终端流，默认 stdin。

    Returns:
        规范化前的 termios 属性；非 TTY 或无法读写属性时为 None。
    """
    target = stream or sys.stdin
    if termios is None:
        return None
    try:
        fd = target.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    try:
        if not os.isatty(fd):
            return None
        saved = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):
        return None
    attrs = list(saved)
    attrs[0] |= termios.ICRNL  # 输入的 CR 翻译为 NL，回车才能结束一行
    attrs[3] |= termios.ICANON | termios.ECHO | termios.ISIG  # 行缓冲、回显、Ctrl+C
    try:
        # TCSADRAIN 等待挂起输出排空，避免与刚写出的提示串竞争。
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    except (OSError, termios.error):
        return None
    return saved


@contextmanager
def canonical_line_input(stream: TextIO | None = None) -> Iterator[None]:
    """读取期间保证 canonical 行输入，退出时还原原有终端属性。"""
    target = stream or sys.stdin
    saved = normalize_line_input(target)
    try:
        yield
    finally:
        if saved is not None and termios is not None:
            try:
                termios.tcsetattr(target.fileno(), termios.TCSADRAIN, saved)
            except (AttributeError, OSError, ValueError, termios.error):
                pass


async def read_console_line(
    prompt: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> str:
    """在线程中读取一行，避免阻塞 asyncio 事件循环。"""
    source = input_stream or sys.stdin
    target = output_stream or sys.stdout
    # 先规范化再写提示：残留的 raw 模式会让回车无法结束这一行。
    with canonical_line_input(source):
        target.write(prompt)
        target.flush()
        line = await asyncio.to_thread(source.readline)
    if line == "":
        raise EOFError
    return line.rstrip("\r\n")


class PlainFrontend:
    """管道和 CI 环境使用的串行输入输出。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        reader: LineReader | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.reader = reader or read_console_line

    def write(self, content: str | Text, end: str = "") -> None:
        value = content.plain if isinstance(content, Text) else content
        self.stream.write(value + end)
        self.stream.flush()

    async def read_input(self, prompt: str, default: str = "") -> str:
        value = await self.reader(prompt)
        return value if value else default

    async def read_permission(self, tool_name: str, detail: str, reason: str = "") -> str:
        prompt = f"\n工具请求权限\n工具: {tool_name}\n内容: {detail}\n"
        if reason:
            prompt += f"智能权限 · {tool_title(tool_name)} · 需确认({reason[:60]})\n"
        self.write(prompt)
        answer = (await self.reader("允许一次？[y/N] ")).strip().lower()
        return "yes" if answer in {"y", "yes"} else "deny"

    async def read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        descriptions: list[str] | None = None,
    ) -> str:
        if prompt:
            self.write(prompt + "\n")
        for index, (_value, label) in enumerate(options, 1):
            self.write(f"{index}. {label}\n")
            if descriptions and index - 1 < len(descriptions) and descriptions[index - 1]:
                self.write(f"   {descriptions[index - 1]}\n")
        answer = (await self.reader(f"选择 [{default_index + 1}]: ")).strip()
        if not answer:
            index = default_index
        elif answer.isdigit():
            index = int(answer) - 1
        else:
            return ""
        return options[index][0] if 0 <= index < len(options) else ""

    async def read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
    ) -> str:
        if prompt:
            self.write(prompt + "\n")
        for index, (_value, label) in enumerate(options, 1):
            self.write(f"{index}. {label}\n")
            if descriptions and index - 1 < len(descriptions) and descriptions[index - 1]:
                self.write(f"   {descriptions[index - 1]}\n")
        answer = (await self.reader(f"{input_placeholder or '选择编号或输入回答'}: ")).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return json.dumps({"choice": options[int(answer) - 1][0], "text": ""})
        if answer:
            return json.dumps({"choice": "", "text": answer}, ensure_ascii=False)
        if options and 0 <= default_index < len(options):
            return json.dumps({"choice": options[default_index][0], "text": ""})
        return ""

    async def read_form(self, prompt: str, questions: list[FormQuestion]) -> str:
        if prompt:
            self.write(prompt + "\n")
        answers: list[str] = []
        for question in questions:
            if question.options:
                if question.multi_select:
                    answers.append(await self._read_multi_choice(question))
                else:
                    answers.append(await self.read_choice(question.question, question.options, 0, question.descriptions))
            else:
                answers.append((await self.reader(f"{question.question}: ")).strip())
        discussion = (await self.reader("补充讨论（回车跳过）: ")).strip()
        return json.dumps({"answers": answers, "discussion": discussion}, ensure_ascii=False)

    async def _read_multi_choice(self, question: FormQuestion) -> str:
        options = question.options or []
        self.write(question.question + "\n")
        for index, (_value, label) in enumerate(options, 1):
            self.write(f"{index}. {label}\n")
        answer = (await self.reader("多选（逗号分隔编号，或输入自定义回答）: ")).strip()
        tokens = [item.strip() for item in answer.replace("，", ",").split(",") if item.strip()]
        if tokens and all(item.isdigit() and 1 <= int(item) <= len(options) for item in tokens):
            seen: set[int] = set()
            values: list[str] = []
            for token in tokens:
                index = int(token) - 1
                if index not in seen:
                    seen.add(index)
                    values.append(options[index][0])
            return "、".join(values)
        return answer
