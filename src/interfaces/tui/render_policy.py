"""Textual TUI 的渲染频率与内存预算。"""

from __future__ import annotations

from dataclasses import dataclass

from src.common.stream_policy import StreamFlushPolicy


@dataclass(frozen=True, slots=True)
class TuiRenderPolicy(StreamFlushPolicy):
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
    def stream_interval(self, length: int) -> float:
        return self.interval(length)
