"""前台渲染与会话检查点共用的流式刷新节奏。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamFlushPolicy:
    """按累计流式长度选择有界的刷新间隔。

    前台渲染与会话落盘共用同一组档位，避免两条路径各自维护一套阈值：正文越长，
    刷新（以及追加式日志的检查点）越稀疏，短正文则保持最短间隔。
    """

    stream_medium_chars: int = 16_000
    stream_large_chars: int = 64_000
    stream_short_interval: float = 0.25
    stream_medium_interval: float = 0.5
    stream_large_interval: float = 1.0

    def interval(self, length: int) -> float:
        """返回给定流式长度对应的刷新间隔（秒）。

        Args:
            length: 当前流式正文的字符数。

        Returns:
            对应的刷新间隔秒数。
        """
        if length > self.stream_large_chars:
            return self.stream_large_interval
        if length > self.stream_medium_chars:
            return self.stream_medium_interval
        return self.stream_short_interval
