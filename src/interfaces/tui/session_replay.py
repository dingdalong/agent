"""会话记录到历史条目与纯文本行的回放投影。

TTY 历史区与非 TTY 降级日志都从这里取回放结果，文本来自 `event_lines`，
因此回放与实时输出逐字一致（除非 TTY 的倒计时提示）。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.common.session_state import SessionRecord
from src.interfaces.tui.event_lines import blocks_for_view
from src.interfaces.tui.history_log import HistoryEntry, strip_trailing_newlines


def history_entries(
    records: Sequence[SessionRecord],
    *,
    resolver: object | None = None,
) -> list[HistoryEntry]:
    """把会话记录投影为单控件历史源，条目 id 由记录 id 派生。

    Args:
        records: 会话记录序列。
        resolver: 可选内容解析器，用于重建被引用化的展示正文。

    Returns:
        历史条目列表。
    """
    entries: list[HistoryEntry] = []
    for record in records:
        for block in blocks_for_view(record, resolver=resolver):
            content = strip_trailing_newlines(block.content)
            if isinstance(content, str) and not content:
                continue
            entries.append(HistoryEntry(
                content,
                markdown=block.markdown and isinstance(content, str),
                style=block.style,
                spacing=block.spacing,
                id=f"{record.id}:{len(entries)}",
            ))
    return entries


def plain_lines(records: Sequence[SessionRecord], *, resolver: object | None = None) -> list[str]:
    """把会话记录投影为无 ANSI 的纯文本行。

    Args:
        records: 会话记录序列。
        resolver: 可选内容解析器，用于重建被引用化的展示正文。

    Returns:
        纯文本行列表。
    """
    lines: list[str] = []
    for record in records:
        for block in blocks_for_view(record, resolver=resolver):
            text = strip_trailing_newlines(block.plain)
            if text:
                lines.append(text)
    return lines
