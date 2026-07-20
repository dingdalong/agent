"""Agent list and read-only transcript interaction state."""

from __future__ import annotations

import json
from dataclasses import dataclass

from prompt_toolkit.formatted_text import ANSI
from rich.text import Text

from src.interfaces.markdown_renderer import render_markdown
from src.interfaces.status_presenter import present_agent

from src.interfaces.agent_view_store import AgentSnapshot, AgentViewStore
from src.interfaces.inline.runtime import InteractionMode


@dataclass(frozen=True, slots=True)
class TranscriptRestore:
    """Input state restored after closing a live transcript."""

    mode: InteractionMode
    text: str
    cursor_position: int


class AgentPanelController:
    """Own agent selection, transcript scroll, and live-view restoration."""

    def __init__(self, store: AgentViewStore) -> None:
        """Initialize empty agent-panel interaction state.

        Args:
            store: Shared source of snapshots and transcript content.

        Returns:
            None.
        """
        self.store = store
        self.selected_index = 0
        self.viewing_uuid: str | None = None
        self.scroll = 0
        self.invoked = False
        self.transcript_cache: tuple[tuple, list[str]] | None = None
        self.message_cache: tuple[tuple, list[str]] | None = None
        self._restore: TranscriptRestore | None = None

    def active_snapshots(self) -> list[AgentSnapshot]:
        """Return active list rows from the shared Store.

        Returns:
            Stable active agent snapshots.
        """
        return self.store.active_agent_snapshots()

    def open_live(
        self,
        uuid: str,
        mode: InteractionMode,
        text: str,
        cursor_position: int,
    ) -> None:
        """Open a read-only live transcript and save exact input state.

        Args:
            uuid: Viewed agent UUID.
            mode: Interaction mode restored on close.
            text: Buffer text restored on close.
            cursor_position: Buffer cursor restored on close.

        Returns:
            None.
        """
        self.viewing_uuid = uuid
        self.scroll = 0
        self.invoked = False
        self._restore = TranscriptRestore(mode, text, cursor_position)

    def close_live(self) -> TranscriptRestore | None:
        """Close a live transcript and return its saved input state.

        Returns:
            Saved input state, or None when no live view was opened.
        """
        restore = self._restore
        self.viewing_uuid = None
        self.scroll = 0
        self._restore = None
        return restore


_AGENT_LIST_MAX_ROWS = 8
_TRANSCRIPT_PANEL_ROWS = 12


class AgentPanelActions:
    """Render agent rows and read-only transcript panels."""

    def _render_agent_list(self) -> ANSI:
        """渲染 agent 列表（每 agent 一行），供 agent_list_window 使用。

        主 agent 行置顶，其余子 agent 按插入序。每行格式：
        <标记> <agent_type> <uuid8> <状态> <token> · 上下文 <used>(<pct>%) · <elapsed>s
        选中行反显（列表聚焦时）。行数 > _AGENT_LIST_MAX_ROWS 时按选中项裁出可视窗口段，
        并在上/下方按需补一行「↑/↓ 还有 N 个」滚动指示（聚焦与否都显示）。

        Returns:
            可作为 Window 内容的 ANSI（多行）。
        """
        rows = self._agent_view_store.active_agent_snapshots()
        if not rows:
            return ANSI("")

        # 滑动窗口：行数 > _AGENT_LIST_MAX_ROWS 时按选中项裁取可见段
        max_visible = _AGENT_LIST_MAX_ROWS
        visible_rows = list(rows)
        start = 0
        if len(visible_rows) > max_visible:
            start = max(0, min(self._agent_selected_index - 3, len(visible_rows) - max_visible))
            # 夹取：确保 select 在 [start, start+max_visible) 内
            if self._agent_selected_index >= start + max_visible:
                start = self._agent_selected_index - max_visible + 1
            elif self._agent_selected_index < start:
                start = self._agent_selected_index
            start = max(0, min(start, len(visible_rows) - max_visible))
            visible_rows = visible_rows[start:start + max_visible]

        # 被裁掉的上/下方 agent 数（未裁剪时均为 0）
        hidden_above = start
        hidden_below = len(rows) - (start + len(visible_rows))

        # 列表是否已聚焦（选中行反显）
        focused = (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        )

        lines: list[Text] = []
        if hidden_above > 0:
            up = Text()
            up.append(f"  ↑ 还有 {hidden_above} 个", style="cyan")
            lines.append(up)
        for i, row in enumerate(visible_rows):
            actual_idx = start + i
            is_selected = focused and actual_idx == self._agent_selected_index

            marker = "⏺" if row.is_main else "◯"

            # 整行样式：选中时反显
            style = "reverse" if is_selected else ""

            line = Text()
            line.append(f"{marker} ", style=style)
            if row.is_main:
                # 主 agent 行只显示标记 + 类型（其输出已在滚动区实时可见）
                line.append(row.agent_type, style=style)
            else:
                # 子 agent 行使用共享 Presenter，与历史摘要和转录标题同源。
                line = present_agent(row, base_style=style)

            lines.append(line)

        if hidden_below > 0:
            dn = Text()
            dn.append(f"  ↓ 还有 {hidden_below} 个", style="cyan")
            lines.append(dn)

        text = Text()
        for j, ln in enumerate(lines):
            text.append(ln)
            if j < len(lines) - 1:
                text.append("\n")

        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _viewing_row(self) -> AgentSnapshot | None:
        """返回当前查看的 agent 快照。

        Returns:
            匹配的 AgentSnapshot；未查看或已从历史逐出时返回 None。
        """
        if self._viewing_uuid is None:
            return None
        return self._agent_view_store.agent_snapshot(self._viewing_uuid)

    def _render_transcript_header(self) -> ANSI:
        """渲染转录面板顶部标题行：「── <标题>（状态）── <跟随态> · ↑/↓ 滚动 · <退出提示>」。

        标题始终从当前 AgentSnapshot 经共享 Presenter 现场生成；历史与实时使用同一指标文本。
        跟随态：_view_scroll==0 显示「实时」，否则显示「已上滚 N 行」。

        Returns:
            可作为 Window 内容的 ANSI（单行标题）。
        """
        row = self._viewing_row()
        if row is not None:
            label = present_agent(row).plain
        else:
            uid8 = self._viewing_uuid.split("-")[0] if self._viewing_uuid else ""
            label = f"◯ {uid8}  已结束"
        exit_hint = "Esc 返回列表" if self._viewing_invoked else "Esc 关闭"
        follow = "实时" if self._view_scroll == 0 else f"已上滚 {self._view_scroll} 行"
        header = Text()
        header.append("── ", style="bright_black")
        header.append(label, style="bold")
        header.append(" ──  ", style="bright_black")
        header.append(follow, style="cyan")
        header.append(f"  ·  ↑/↓ 滚动 · {exit_hint}", style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(header, end="")
        return ANSI(capture.get())

    def _render_transcript_panel(self) -> ANSI:
        """渲染转录面板内容：把当前查看 agent 的内容按宽度折行后，切出贴底（或上滚后）的可视段。

        数据源按「是否已有完整原始记录」决定，与调起方式（实时列表 / `/agents`）无关：
        - 已完成（messages_provider 返回非空）→ 渲染完整原始消息（_message_lines）；
        - 运行中（尚无原始快照）→ 渲染实时增量分段（_transcript_lines），借 100ms 重绘实现 tail -f。
        故实时查看一个运行中的子 agent，待其完成后面板会就地升级为完整原始记录。
        _view_scroll==0 时恒切末段；_view_scroll 在此就地夹取到合法上界，使越界的 ↑ 自然停在顶部。

        Returns:
            可作为 Window 内容的 ANSI（至多 _TRANSCRIPT_PANEL_ROWS 行）。
        """
        if self._viewing_uuid is None:
            return ANSI("")
        uuid = self._viewing_uuid
        messages = self._agent_view_store.transcript_messages(uuid)
        if messages:  # 已完成：完整原始消息
            lines = self._message_lines(uuid, messages)
        else:  # 运行中：实时增量分段
            lines = self._transcript_lines(
                uuid,
                self._agent_view_store.transcript_segments(uuid),
            )
        max_scroll = max(0, len(lines) - _TRANSCRIPT_PANEL_ROWS)
        self._view_scroll = min(self._view_scroll, max_scroll)  # 就地夹取上界
        start = max_scroll - self._view_scroll
        return ANSI("\n".join(lines[start:start + _TRANSCRIPT_PANEL_ROWS]))

    def _transcript_lines(self, uuid: str, segments: list[tuple[str, str]]) -> list[str]:
        """把转录分段渲染为可滚动的 ANSI 行列表：response/thinking 走 Markdown、tool 走纯文本。

        带缓存：签名 = (uuid, 段数, 各段文本总长, 渲染宽度)；签名不变直接复用已渲染行，
        仅在转录增长/切换 agent/改宽度时重渲，避免对增长缓冲每帧重复解析 Markdown。

        Args:
            uuid: 当前查看的 agent uuid（参与缓存签名，切换 agent 即失效）。
            segments: 转录分段列表，每项为 (kind, text)。
        Returns:
            渲染后的 ANSI 文本按 "\\n" 切分的行列表。
        """
        signature = (uuid, len(segments), sum(len(text) for _, text in segments), self._render_width)
        if self._transcript_cache is not None and self._transcript_cache[0] == signature:
            return self._transcript_cache[1]
        parts: list[str] = []
        for kind, text in segments:
            if kind == "response":
                parts.append(render_markdown(text, width=self._render_width))
            elif kind == "thinking":
                parts.append(render_markdown(text, width=self._render_width, base_style="dim"))
            else:  # tool：工具调用 chrome 行，纯文本渲染
                with self._status_console.capture() as capture:
                    self._status_console.print(Text(text), end="")
                parts.append(capture.get())
        lines = "".join(parts).split("\n")
        self._transcript_cache = (signature, lines)
        return lines

    def _message_lines(self, uuid: str, messages: list[dict]) -> list[str]:
        """把某子 agent 的完整原始消息（Agent.history）渲染为可滚动的 ANSI 行列表（已完成 agent 查看用）。

        逐条按 role 完整渲染、不截断：user→「▶ 用户」+原文；assistant→「● 助手」+思考(dim)+正文+
        每个工具调用「⚙ <工具名>」+美化 JSON 参数；tool→「⚙ 结果 (<tool_call_id 末段>)」+返回原文。
        带缓存：签名 = (uuid, 消息条数, 内容总长, 渲染宽度)；签名不变直接复用，避免每帧重渲大 history。

        Args:
            uuid: 目标子 agent 的 uuid（参与缓存签名，切换 agent 即失效）。
            messages: 该 agent 的完整原始消息列表（调用方已保证非空）。
        Returns:
            渲染后的 ANSI 文本按 "\\n" 切分的行列表。
        """
        content_len = sum(len(str(m.get("content") or "")) for m in messages)
        signature = (uuid, len(messages), content_len, self._render_width)
        if self._message_cache is not None and self._message_cache[0] == signature:
            return self._message_cache[1]
        text = Text()
        for msg in messages:
            self._append_message(text, msg)
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        lines = capture.get().split("\n")
        self._message_cache = (signature, lines)
        return lines

    def _append_message(self, text: Text, msg: dict) -> None:
        """把单条原始消息 dict 按 role 完整追加到给定 Rich Text（供 _message_lines 逐条调用）。

        Args:
            text: 目标 Rich Text，原地追加渲染内容。
            msg: 单条消息 dict（OpenAI 归一化 schema：role/content/tool_calls/reasoning* 等）。
        """
        role = msg.get("role", "")
        if role == "user":
            text.append("\n▶ 用户\n", style="bold cyan")
            text.append(str(msg.get("content") or ""))
            text.append("\n")
        elif role == "assistant":
            text.append("\n● 助手\n", style="bold green")
            thinking = msg.get("reasoning_content") or msg.get("reasoning")
            if thinking:
                text.append(str(thinking), style="dim")
                text.append("\n")
            content = msg.get("content")
            if content:
                text.append(str(content))
                text.append("\n")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {})
                text.append(f"  ⚙ {fn.get('name', '')}\n", style="yellow")
                text.append(self._format_tool_arguments(fn.get("arguments", "")))
                text.append("\n")
        elif role == "tool":
            tail = str(msg.get("tool_call_id") or "").split("-")[0]
            text.append(f"\n  ⚙ 结果 ({tail})\n", style="bright_black")
            text.append(str(msg.get("content") or ""))
            text.append("\n")
        else:  # 未知 role：原样标注，保证不丢信息
            text.append(f"\n[{role}]\n", style="bright_black")
            text.append(str(msg.get("content") or ""))
            text.append("\n")

    def _format_tool_arguments(self, arguments: str) -> str:
        """把工具调用的 arguments（JSON 串）美化为缩进文本；解析失败回退原串。

        Args:
            arguments: assistant 消息里工具调用的 function.arguments 原始字符串。
        Returns:
            美化后的多行 JSON 文本，或原始串（非合法 JSON 时）。
        """
        try:
            return json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            return str(arguments)

    def _has_sub_agents(self) -> bool:
        """检查当前是否有子 agent（列表是否应该可见）。

        Returns:
            True 当 rows_provider 已装配且存在非 main 的行。
        """
        try:
            return any(
                not snapshot.is_main and snapshot.running
                for snapshot in self._agent_view_store.active_agent_snapshots()
            )
        except Exception:
            return False

