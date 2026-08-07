"""TUI 异常降级时用于普通文字回放的历史日志。"""

from __future__ import annotations

from io import StringIO

from rich.text import Text


class PlainHistoryJournal:
    """顺序记录已展示内容，不保存任何可恢复的控件状态。"""

    def __init__(self) -> None:
        self._buffer = StringIO()
        self._at_line_start = True
        self._active_stream: str | None = None

    def append_entry(self, value: str | Text) -> None:
        text = value.plain if isinstance(value, Text) else value
        self._finish_active_stream()
        if not self._at_line_start:
            self._write("\n")
        self._write(text)
        if not self._at_line_start:
            self._write("\n")

    def start_stream(self, stream_id: str, initial: str = "") -> None:
        self._finish_active_stream()
        if not self._at_line_start:
            self._write("\n")
        self._active_stream = stream_id
        self._write(initial)

    def append_stream(self, stream_id: str, content: str) -> None:
        if not content:
            return
        if self._active_stream != stream_id:
            self.start_stream(stream_id)
        self._write(content)

    def end_stream(self, stream_id: str) -> None:
        if self._active_stream != stream_id:
            return
        self._finish_active_stream()

    def snapshot(self) -> str:
        value = self._buffer.getvalue()
        if value and not value.endswith("\n"):
            return value + "\n"
        return value

    def clear(self) -> None:
        """清空当前会话的降级回放缓存。"""
        self._buffer = StringIO()
        self._at_line_start = True
        self._active_stream = None

    def _finish_active_stream(self) -> None:
        if self._active_stream is None:
            return
        if not self._at_line_start:
            self._write("\n")
        self._active_stream = None

    def _write(self, value: str) -> None:
        if not value:
            return
        self._buffer.write(value)
        self._at_line_start = value.endswith("\n")
