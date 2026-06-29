"""Streaming Markdown renderer for terminal assistant responses."""

from __future__ import annotations

import io
import re
import shutil

from rich.console import Console
from rich.markdown import Markdown


def render_markdown(
    md: str,
    *,
    width: int | None = None,
    color_system: str | None = "standard",
    code_theme: str = "monokai",
    base_style: str = "",
) -> str:
    """一次性把一段 Markdown 渲染为 ANSI 字符串（无流式缓冲）。

    Args:
        md: 待渲染的 Markdown 源文本。
        width: 渲染宽度；None 时按终端宽度自动探测。
        color_system: Rich 色彩系统；None 表示不着色（不产生 ANSI 控制码）。
        code_theme: 代码块高亮主题。
        base_style: 整体叠加到正文上的 Rich 样式（如 "dim"），烘焙进返回的 ANSI；空串表示不叠加。
    Returns:
        渲染后的 ANSI 字符串（结尾不补换行）。
    """
    output = io.StringIO()
    console = Console(
        file=output,
        force_terminal=color_system is not None,
        color_system=color_system,
        width=width if width is not None else shutil.get_terminal_size(fallback=(88, 24)).columns,
        legacy_windows=False,
    )
    # 把 base_style 整体叠加到本块（逐片段 Segment.apply_style，彩色片段叠加后保留色相），烘焙进 ANSI。
    console.print(Markdown(md, code_theme=code_theme), end="", style=base_style or None)
    return output.getvalue()


class MarkdownStreamRenderer:
    """Render completed Markdown blocks without reparsing prior output."""

    _FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
    _ATOMIC_LINE_RE = re.compile(
        r"^\s*(#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|>\s*|[-*_]{3,}\s*$)"
    )

    def __init__(
        self,
        *,
        width: int | None = None,
        color_system: str | None = "standard",
        code_theme: str = "monokai",
        base_style: str = "",
    ) -> None:
        # base_style：渲染时整体叠加到 markdown 之上的 Rich 样式（如思考流的 "dim" 整体压暗），
        # 烘焙进返回的 ANSI 字符串本身；空串表示不叠加。烘焙在本类自有的临时 Console 上完成，
        # 故调用方用 Live 打印结果时无需再传 style=（避免污染 Live 底部状态栏，见 inline_ui._print_ansi）。
        self.width = width
        self.color_system = color_system
        self.code_theme = code_theme
        self.base_style = base_style
        self._buffer = ""

    def append(self, content: str) -> list[str]:
        if not content:
            return []

        self._buffer += content
        return self._drain_completed_blocks()

    def flush(self) -> list[str]:
        if not self._buffer:
            return []

        block = self._buffer
        self._buffer = ""
        if not block.strip():
            return []
        return [self._render_markdown(block)]

    def reset(self) -> None:
        self._buffer = ""

    def _drain_completed_blocks(self) -> list[str]:
        chunks: list[str] = []
        while self._buffer:
            block_end = self._completed_block_end(self._buffer)
            if block_end is None:
                break

            block = self._buffer[:block_end]
            self._buffer = self._buffer[block_end:]
            if block.strip():
                chunks.append(self._render_markdown(block))
        return chunks

    def _completed_block_end(self, text: str) -> int | None:
        offset = 0
        in_fence = False
        fence_marker = ""

        for line in text.splitlines(keepends=True):
            line_end = offset + len(line)
            if not line.endswith("\n"):
                return None

            stripped = line.strip()
            fence_match = self._FENCE_RE.match(line)
            if fence_match is not None:
                marker = fence_match.group(1)
                if in_fence and marker.startswith(fence_marker[0]) and len(marker) >= len(fence_marker):
                    return line_end
                if not in_fence:
                    in_fence = True
                    fence_marker = marker
                offset = line_end
                continue

            if in_fence:
                offset = line_end
                continue

            if not stripped:
                return line_end

            if self._ATOMIC_LINE_RE.match(line):
                return line_end

            offset = line_end
        return None

    def _render_markdown(self, markdown: str) -> str:
        return render_markdown(
            markdown,
            width=self.width,
            color_system=self.color_system,
            code_theme=self.code_theme,
            base_style=self.base_style,
        )
