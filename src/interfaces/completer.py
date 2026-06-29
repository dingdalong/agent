"""斜杠命令自动补全器 — 输入 `/` 时按前缀过滤 SLASH_COMMANDS 提供补全候选。"""

from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from src.agent.states import SLASH_COMMANDS


class SlashCommandCompleter(Completer):
    """为输入框提供斜杠命令补全：仅在正在输入命令名（以 `/` 开头且不含空格）时产出候选。"""

    def get_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        """按当前输入产出匹配的斜杠命令补全候选。

        Args:
            document: 当前缓冲文档（用其光标前文本判断是否在补全命令名）。
            complete_event: prompt_toolkit 补全事件（未使用）。
        Returns:
            匹配的 Completion 迭代器；非命令名输入场景下为空。
        """
        word = document.text_before_cursor.lstrip()
        # 仅在命令名阶段补全：必须以 `/` 开头且尚未输入空格（一旦带参数即停止弹窗）。
        if not word.startswith("/") or " " in word:
            return
        prefix = word[1:].lower()
        for name, description in SLASH_COMMANDS:
            if name.startswith(prefix):
                yield Completion(
                    text=f"/{name}",
                    start_position=-len(word),
                    display=f"/{name}",
                    display_meta=description,
                )
