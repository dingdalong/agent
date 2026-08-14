"""Textual TUI 的渲染频率与内存预算。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TuiRenderPolicy:
    """集中管理只影响前台渲染的固定保护参数。"""

    history_page_entries: int = 80
    history_window_entries: int = 250
    history_window_chars: int = 200_000
    history_window_lines: int = 4_000
    history_entry_chars: int = 100_000
    history_entry_source_lines: int = 2_000
    history_resize_debounce: float = 0.15
    history_reflow_slice: float = 0.008
    journal_chars: int = 250_000
    activity_interval: float = 0.25
    stream_medium_chars: int = 16_000
    stream_large_chars: int = 64_000
    stream_short_interval: float = 0.25
    stream_medium_interval: float = 0.5
    stream_large_interval: float = 1.0

    def stream_interval(self, length: int) -> float:
        if length > self.stream_large_chars:
            return self.stream_large_interval
        if length > self.stream_medium_chars:
            return self.stream_medium_interval
        return self.stream_short_interval
