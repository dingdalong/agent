"""TUI 异常降级时用于普通文字回放的历史日志。"""

from __future__ import annotations

from collections import deque
from io import StringIO

from rich.text import Text

from src.interfaces.tui.render_policy import TuiRenderPolicy


_TRUNCATION_NOTICE = "[较早历史未回放]\n"
_BUFFER_BLOCK_CHARS = 4_096


class _TailTextBuffer:
    """以固定大小分块保存最近文本，避免超限后反复复制完整尾部。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(0, limit)
        self._blocks: deque[StringIO] = deque()
        self._head_offset = 0
        self._length = 0

    def append(self, value: str) -> bool:
        if not value:
            return False
        truncated = False
        if self._limit == 0:
            truncated = bool(self._length or value)
            self._blocks.clear()
            self._head_offset = 0
            self._length = 0
            return truncated
        if len(value) >= self._limit:
            truncated = self._length > 0 or len(value) > self._limit
            value = value[-self._limit:]
            self._blocks.clear()
            self._head_offset = 0
            self._length = 0
        offset = 0
        while offset < len(value):
            if not self._blocks or self._blocks[-1].tell() >= _BUFFER_BLOCK_CHARS:
                self._blocks.append(StringIO())
            block = self._blocks[-1]
            length = min(_BUFFER_BLOCK_CHARS - block.tell(), len(value) - offset)
            block.write(value[offset:offset + length])
            self._length += length
            offset += length
        return self._trim() or truncated

    def set_limit(self, limit: int) -> bool:
        self._limit = max(0, limit)
        return self._trim()

    def getvalue(self) -> str:
        if not self._blocks:
            return ""
        blocks = [block.getvalue() for block in self._blocks]
        blocks[0] = blocks[0][self._head_offset:]
        return "".join(blocks)

    def tell(self) -> int:
        return self._length

    def _trim(self) -> bool:
        overflow = self._length - self._limit
        if overflow <= 0:
            return False
        while overflow > 0:
            available = self._blocks[0].tell() - self._head_offset
            if overflow >= available:
                self._blocks.popleft()
                self._head_offset = 0
                self._length -= available
                overflow -= available
            else:
                self._head_offset += overflow
                self._length -= overflow
                overflow = 0
        return True


class PlainHistoryJournal:
    """顺序记录已展示内容，不保存任何可恢复的控件状态。"""

    def __init__(self, policy: TuiRenderPolicy | None = None) -> None:
        self._policy = policy or TuiRenderPolicy()
        budget = max(1, self._policy.journal_chars)
        self._completed_buffer = _TailTextBuffer(budget)
        self._entry_chars = 0
        self._truncated = False
        self._active_stream: str | None = None
        self._active_buffer = _TailTextBuffer(budget)
        self._active_chars = 0

    def append_entry(self, value: str | Text) -> None:
        text = value.plain if isinstance(value, Text) else value
        self._finish_active_stream()
        self._append_completed(text)

    def start_stream(self, stream_id: str, initial: str = "") -> None:
        self._finish_active_stream()
        self._active_stream = stream_id
        budget = max(1, self._policy.journal_chars)
        self._active_buffer = _TailTextBuffer(budget)
        if self._active_buffer.append(initial):
            self._truncated = True
        self._active_chars = self._active_buffer.tell()
        self._trim_active_stream()

    def append_stream(self, stream_id: str, content: str) -> None:
        if not content:
            return
        if self._active_stream != stream_id:
            self.start_stream(stream_id)
        if self._active_buffer.append(content):
            self._truncated = True
        self._active_chars = self._active_buffer.tell()
        self._trim_active_stream()

    def end_stream(self, stream_id: str) -> None:
        if self._active_stream != stream_id:
            return
        self._finish_active_stream()

    def snapshot(self) -> str:
        value = self._completed_buffer.getvalue()
        if self._active_stream is not None:
            active = self._active_buffer.getvalue()
            if active:
                value += self._normalize_entry(active)
        value = ("" if not self._truncated else _TRUNCATION_NOTICE) + value
        return value if not value or value.endswith("\n") else value + "\n"

    def clear(self) -> None:
        """清空当前会话的降级回放缓存。"""
        budget = max(1, self._policy.journal_chars)
        self._completed_buffer = _TailTextBuffer(budget)
        self._entry_chars = 0
        self._truncated = False
        self._active_stream = None
        self._active_buffer = _TailTextBuffer(budget)
        self._active_chars = 0

    def _finish_active_stream(self) -> None:
        if self._active_stream is None:
            return
        content = self._active_buffer.getvalue()
        self._active_stream = None
        self._active_buffer = _TailTextBuffer(max(1, self._policy.journal_chars))
        self._active_chars = 0
        self._append_completed(content)

    def _append_completed(self, value: str) -> None:
        if not value:
            return
        normalized = self._normalize_entry(value)
        budget = max(1, self._policy.journal_chars)
        self._completed_buffer.set_limit(budget)
        if self._completed_buffer.append(normalized):
            self._truncated = True
        self._entry_chars = self._completed_buffer.tell()

    def _trim_active_stream(self) -> None:
        budget = max(1, self._policy.journal_chars)
        completed_limit = max(0, budget - self._active_chars)
        if self._completed_buffer.set_limit(completed_limit):
            self._truncated = True
        self._entry_chars = self._completed_buffer.tell()

    @staticmethod
    def _normalize_entry(value: str) -> str:
        return value if value.endswith("\n") else value + "\n"
