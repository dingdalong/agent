"""Shared status-bar controller."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from prompt_toolkit.formatted_text import ANSI

from rich.text import Text

from src.events.types import ToolCallCompleted, ToolCallStarted
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.status_presenter import (
    format_elapsed_time,
    present_agent,
    present_ended_agent,
    present_session_metrics,
)


class StatusBarController:
    """Present Store-backed session or viewed-agent status."""

    def __init__(
        self,
        store: AgentViewStore,
        permission_mode: Callable[[], str],
    ) -> None:
        """Initialize a status controller.

        Args:
            store: Shared session/agent read model.
            permission_mode: Provider for the current root permission mode.

        Returns:
            None.
        """
        self._store = store
        self._permission_mode = permission_mode

    def present(
        self,
        elapsed_seconds: float,
        toggle_available: bool,
        agent_uuid: str | None = None,
    ) -> Text:
        """Build the session or viewed-agent core status line.

        Args:
            elapsed_seconds: Current or last-turn elapsed time.
            toggle_available: Whether to show the Shift+Tab hint.
            agent_uuid: Viewed subagent UUID, or None for session status.

        Returns:
            Rich status text.
        """
        if agent_uuid is not None:
            snapshot = self._store.agent_snapshot(agent_uuid)
            if snapshot is not None:
                return present_agent(snapshot)
            return present_ended_agent(agent_uuid)
        line = Text(self._permission_mode())
        if toggle_available:
            line.append(" (Shift+Tab 切换)", style="bright_black")
        line.append("  ·  ", style="bright_black")
        line.append_text(present_session_metrics(
            self._store.session_snapshot(),
            elapsed_seconds,
        ))
        return line


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 顶部「本轮面板」最多逐条展开的工具行数；超出部分折叠为「… 还有 N 个」。
_ROUND_PANEL_MAX_ROWS = 8


@dataclass(slots=True)
class _RoundEntry:
    """前台 agent 本轮单个工具调用的实时/定稿状态，既驱动顶部面板又供轮边界 flush。"""

    tool_call_id: str  # 工具调用 id，用于把完成事件对回到对应条目
    tool_name: str  # 工具名
    detail: str  # 工具详情（由 permission.tips 模板生成，已 strip）
    started_monotonic: float  # 本工具开始的 monotonic 秒，用于面板实时计时
    status: str = "running"  # running | success | error
    preview: str = ""  # 完成后的结果预览（已 strip，仅 flush 时使用）
    duration: float = 0.0  # 完成后的耗时秒（来自完成事件）


class StatusBarActions:
    """Render and update activity and core status state."""

    def _render_activity(self) -> ANSI:
        """构建实时区活动内容的 ANSI；仅处理态且有活动/本轮工具/重试等待时由其窗口显示。

        首行留空，与上方滚动正文（messages 区）分隔。重试等待中优先渲染倒计时行；否则本轮缓冲
        非空时渲染「本轮面板」（头行统计 + 每工具一行，各自 spinner/✔/✘ + 计时）；否则回退单行
        「spinner + 当前 agent · 活动 (分段整数耗时)」（思考中/回应中/压缩上下文等非工具阶段）。

        Returns:
            可作为 Window 内容的 ANSI（空行 + 倒计时行 / 面板多行 / 单行活动文案）。
        """
        now = time.monotonic()
        status = Text("\n")
        if self._retry_deadline is not None:
            self._append_retry_countdown(status, now)
        elif self._round_entries:
            self._append_round_panel(status, now)
        else:
            frame = _SPINNER_FRAMES[int(now * 10) % len(_SPINNER_FRAMES)]
            step_elapsed = self._activity_elapsed(now)
            status.append(f"{frame} ", style="cyan")
            status.append(
                f"{self._active_agent_name()} · {self._activity} "
                f"({format_elapsed_time(step_elapsed)})",
                style="cyan",
            )
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _append_retry_countdown(self, status: Text, now: float) -> None:
        """把 API 重试倒计时行渲染进给定 Rich Text（黄色，剩余秒向上取整）。

        剩余秒 = 截止 monotonic 减 now 后向上取整并下限为 0，随 100ms 重绘逐秒递减。

        Args:
            status: 目标 Rich Text，原地追加倒计时行。
            now: 当前 monotonic 秒，用于计算剩余秒与 spinner 帧。
        """
        frame = _SPINNER_FRAMES[int(now * 10) % len(_SPINNER_FRAMES)]
        remaining = max(0, math.ceil(self._retry_deadline - now))
        status.append(f"{frame} ", style="yellow")
        status.append(
            f"{self._active_agent_name()} · LLM错误[{self._retry_error_kind}] "
            f"{self._retry_safe_message}，"
            f"{remaining}秒后重试 ({self._retry_attempt}/{self._retry_max})",
            style="yellow",
        )

    def _append_round_panel(self, status: Text, now: float) -> None:
        """把「本轮面板」渲染进给定 Rich Text：头行统计 + 每工具一行（超限折叠）。

        Args:
            status: 目标 Rich Text，原地追加面板内容。
            now: 当前 monotonic 秒，用于运行中工具的实时计时与 spinner 帧。
        """
        entries = self._round_entries
        done = sum(1 for entry in entries if entry.status != "running")
        running = len(entries) - done
        frame = _SPINNER_FRAMES[int(now * 10) % len(_SPINNER_FRAMES)]
        status.append(f"{frame} ", style="cyan")
        status.append(f"本轮 · {len(entries)} 工具（{done} 完成 · {running} 进行中）", style="cyan")
        for entry in entries[:_ROUND_PANEL_MAX_ROWS]:
            status.append("\n")
            self._append_round_entry(status, entry, now, frame)
        hidden = len(entries) - _ROUND_PANEL_MAX_ROWS
        if hidden > 0:
            status.append(f"\n  … 还有 {hidden} 个", style="bright_black")

    def _append_round_entry(self, status: Text, entry: _RoundEntry, now: float, frame: str) -> None:
        """把「本轮面板」单条工具行渲染进给定 Rich Text。

        Args:
            status: 目标 Rich Text，原地追加该工具行。
            entry: 本轮单个工具调用条目。
            now: 当前 monotonic 秒，用于运行中工具的实时计时。
            frame: 当前 spinner 帧字符，运行中工具共用。
        """
        detail = f" {entry.detail}" if entry.detail else ""
        if entry.status == "running":
            elapsed = format_elapsed_time(max(0.0, now - entry.started_monotonic))
            status.append(f"  {frame} ", style="cyan")
            status.append(f"{entry.tool_name}{detail} ({elapsed})", style="cyan")
            return
        ok = entry.status == "success"
        mark_style = "green" if ok else "red"
        status.append(f"  {'✔' if ok else '✘'} ", style=mark_style)
        status.append(
            f"{entry.tool_name}{detail} ({format_elapsed_time(entry.duration)})",
            style=mark_style,
        )

    def _round_append_start(self, event: ToolCallStarted) -> None:
        """把前台 agent 的一次工具开始追加进本轮缓冲（非前台 agent 忽略，其进展走底部列表）。

        缓冲从空转非空时记录本轮所属 agent 身份，供轮边界 flush 的头行使用。

        Args:
            event: 工具调用开始事件。
        """
        if event.caller_uuid != self._agent_view_store.foreground_uuid:
            return
        if not self._round_entries:
            self._round_agent_type = event.caller_agent_type
            self._round_agent_uuid = event.caller_uuid
        self._round_entries.append(_RoundEntry(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            detail=event.detail.strip(),
            started_monotonic=time.monotonic(),
        ))

    def _round_settle(self, event: ToolCallCompleted) -> None:
        """按 tool_call_id 把本轮缓冲中对应条目落定为成功/失败并记录预览与耗时。

        Args:
            event: 工具调用完成事件。
        """
        for entry in self._round_entries:
            if entry.tool_call_id == event.tool_call_id:
                entry.status = "success" if event.status == "success" else "error"
                entry.preview = (event.result_preview or "").strip()
                entry.duration = event.duration_seconds
                return

    def _round_flush(self) -> None:
        """在轮边界把本轮缓冲一次性定稿为 scrollback 分组块，随后清空缓冲。缓冲空则 no-op。

        头行 `● {agent} · 本轮 N 工具`；每条目一行，按提交序：已落定 → `✔/✘ {工具} {详情} ⎿ {预览首行} (耗时)`
        （成功绿、失败红且附剩余预览行）；仍运行（仅中断残留）→ dim `⋯ {工具} {详情} 已中断`。
        """
        if not self._round_entries:
            return
        agent = self._agent_label(self._round_agent_type, self._round_agent_uuid)
        header = Text("● ", style="bold")
        if agent:
            header.append(f"{agent} ", style="cyan")
        header.append(f"· 本轮 {len(self._round_entries)} 工具", style="bold")
        self._print_rich(header)
        for entry in self._round_entries:
            self._flush_round_entry(entry)
        self._round_entries = []
        self._round_agent_type = None
        self._round_agent_uuid = None

    def _flush_round_entry(self, entry: _RoundEntry) -> None:
        """把本轮单个工具条目定稿输出为一行（失败时附剩余预览行）到 scrollback。

        Args:
            entry: 本轮单个工具调用条目。
        """
        detail = entry.detail
        if entry.status == "running":  # 仅 Ctrl+C 中断时残留
            line = Text("  ⋯ ", style="bright_black")
            line.append(entry.tool_name, style="bright_black")
            if detail:
                line.append(f"  {detail}", style="bright_black")
            line.append("  已中断", style="bright_black")
            self._print_rich(line)
            return
        ok = entry.status == "success"
        style = "green" if ok else "red"
        preview_lines = entry.preview.splitlines()
        first = preview_lines[0] if preview_lines else ("完成" if ok else "失败")
        line = Text(f"  {'✔' if ok else '✘'} ", style=style)
        line.append(entry.tool_name, style=style)
        if detail:
            line.append(f"  {detail}", style=style)
        line.append("  ⎿ ", style="bright_black")
        line.append(first, style=style)
        line.append(f"  ({entry.duration:.2f}s)", style="bright_black")
        self._print_rich(line)
        if not ok and len(preview_lines) > 1:
            # 失败时保留完整预览（首行之后的剩余内容），便于排查
            self._print_rich("\n".join(preview_lines[1:]), style="red")

    def _render_separator(self) -> ANSI:
        """构建一行占满终端宽度的暗色分割线 ANSI。

        Returns:
            可作为 Window 内容的 ANSI（单行分割线）。
        """
        separator = Text("─" * self._render_width, style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(separator, end="")
        return ANSI(capture.get())

    def _render_core_status(self) -> ANSI:
        """构建底部会话状态或查看中子 agent 状态的 ANSI。

        耗时为全会话累计有效耗时（已完成回合的累计 + 本回合实时，均剔除纯人工等待），与会话 token 累计一致。
        处理态与中途弹窗态（回合计时中且非输入态）叠加本回合实时段：有工具在算时走动、纯人工等待时冻结；
        处理态另追加「Ctrl+C 中断」提示。输入态只显示已完成回合的累计值（冻结），不带中断提示。
        查看子 agent 时改用其快照并隐藏全部主流程操作提示。

        Returns:
            可作为 Window 内容的 ANSI（单行核心状态）。
        """
        viewing_agent = self._viewing_uuid is not None
        processing = (
            not viewing_agent
            and self._mode == "processing"
            and (bool(self._activity) or self._retry_deadline is not None)
        )
        now = time.monotonic()
        if self._turn_started_monotonic is not None and self._mode != "input":
            elapsed = self._session_elapsed_accumulated + self._turn_elapsed(now)
        else:
            elapsed = self._session_elapsed_accumulated
        status = Text()
        self._append_core_status(status, elapsed)
        if processing:
            status.append("  ·  ", style="bright_black")
            status.append("Ctrl+C 中断", style="bright_black")
        if not viewing_agent and self._has_sub_agents():
            status.append("  ·  ", style="bright_black")
            status.append("↓查看 agent", style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _append_core_status(self, line: Text, elapsed: float) -> None:
        """把当前会话或查看中子 agent 的核心状态段原地追加。

        Args:
            line: 目标 Rich Text，原地追加内容。
            elapsed: 主会话累计有效耗时；查看子 agent 时由其生命周期耗时取代。

        Returns:
            None.
        """
        line.append_text(self._status_bar.present(
            elapsed,
            self._permission_mode_toggle_handler is not None,
            self._viewing_uuid,
        ))

    def _turn_elapsed(self, now: float) -> float:
        """本回合有效耗时：自起点到 now、扣除本回合累计纯人工等待暂停；回合未开始返回 0.0。

        Args:
            now: 当前 monotonic 秒。
        Returns:
            有效耗时秒数（暂停中该值与 now 无关，天然冻结在暂停起点值）。
        """
        if self._turn_started_monotonic is None:
            return 0.0
        return max(0.0, now - self._turn_started_monotonic - self._turn_clock.paused_seconds(now))

    def _activity_elapsed(self, now: float) -> float:
        """本步（当前活动）有效耗时：自本步起点到 now、扣除本步开始后新增的纯人工等待暂停；未开始返回 0.0。

        Args:
            now: 当前 monotonic 秒。
        Returns:
            本步有效耗时秒数。
        """
        if self._activity_started_monotonic is None:
            return 0.0
        paused_since = self._turn_clock.paused_seconds(now) - self._activity_paused_baseline
        return max(0.0, now - self._activity_started_monotonic - paused_since)

    @contextmanager
    def _human_interaction(self) -> Iterator[asyncio.Future[str]]:
        """包装中途弹窗的交互上下文，在等待期间标记回合处于人工等待（供耗时暂停）。

        进入 runtime.interaction() 成功后才 enter_human_wait，避免嵌套 backstop 抛错时误增计数；
        退出时无论正常或中断都 exit_human_wait。

        Yields:
            由输入/菜单/表单流程落定的交互 future。
        """
        with self._runtime.interaction() as future:
            self._turn_clock.enter_human_wait()
            try:
                yield future
            finally:
                self._turn_clock.exit_human_wait()

    def _begin_retry_countdown(
        self,
        error_kind: str,
        safe_message: str,
        attempt: int,
        max_attempts: int,
        wait_seconds: float,
    ) -> None:
        """进入处理态并开始 API 重试倒计时，驱动活动区实时倒计时行。

        记录截止 monotonic（now + wait_seconds）与安全错误信息；首次记录本回合处理起点。
        倒计时期间保持既有 `_activity` 文案不变，收到下一轮 LLMCallStarted 的 `_set_activity`
        时倒计时被清除。

        Args:
            error_kind: 稳定的 LLM 错误类别。
            safe_message: 不含请求体、响应体和凭据的错误摘要。
            attempt: 已失败的尝试序号（1 基）。
            max_attempts: 允许的最大尝试次数。
            wait_seconds: 本次等待秒数（含抖动的原始浮点值）。
        """
        now = time.monotonic()
        self._retry_deadline = now + wait_seconds
        self._retry_error_kind = error_kind
        self._retry_safe_message = safe_message
        self._retry_attempt = attempt
        self._retry_max = max_attempts
        self._mode = "processing"
        if self._turn_started_monotonic is None:
            self._turn_started_monotonic = now

    def _set_activity(self, activity: str) -> None:
        """进入处理态并设置当前活动文案，驱动底部状态条 spinner / 活动显示。

        首次记录本回合处理起点；活动切换或刚退出重试等待时重置本步耗时起点。

        Args:
            activity: 当前活动文案（如"思考中"、"回应中"、工具名；空串为提交后的空闲态）。
        """
        left_retry = self._retry_deadline is not None
        self._clear_retry_status()
        activity_changed = activity != self._activity
        self._activity = activity
        self._mode = "processing"
        now = time.monotonic()
        if self._turn_started_monotonic is None:
            self._turn_started_monotonic = now
        if activity_changed or left_retry:
            self._activity_started_monotonic = now  # 活动切换或退出重试时重置本步耗时起点
            # 记录本步起始时的累计暂停基线，本步耗时只剔除其后的暂停；
            # 弹窗期间消费者阻塞、不会触发活动切换，故此刻必不在暂停中。
            self._activity_paused_baseline = self._turn_clock.paused_seconds(now)

    def _clear_retry_status(self) -> None:
        """清空重试倒计时、安全错误摘要与尝试序号。

        Returns:
            None。
        """
        self._retry_deadline = None
        self._retry_error_kind = ""
        self._retry_safe_message = ""
        self._retry_attempt = 0
        self._retry_max = 0

    def _reset_turn_status(self) -> None:
        """清零单回合状态：整轮/本步耗时起点、暂停累计、活动文案与当前 agent。在每次进入输入阶段时调用。

        全会话累计耗时 `_session_elapsed_accumulated` 与 token 统计同为会话级、跨回合保留，
        仅由 /clear（controller.reload / Store reset）归零，均不属于单回合状态。
        """
        self._turn_started_monotonic = None
        self._activity_started_monotonic = None
        self._activity_paused_baseline = 0.0
        self._turn_clock.reset()
        self._activity = ""
        self._clear_retry_status()
        self._current_agent_type = None
        self._current_agent_uuid = None
        # 防御性清空本轮缓冲：正常流程下 Trigger B 已在进入输入态前 flush，此处兜底避免残留跨回合。
        self._round_entries = []
        self._round_agent_type = None
        self._round_agent_uuid = None

    def _active_agent_name(self) -> str:
        """返回活动行要显示的当前 agent 名：主 agent 与子 agent 均显示其 agent_type；
        agent_type 为空时（回合起始 reset 后的短暂空窗）回退「助手」。

        Returns:
            agent 显示名，如「main」「coder」「explore」或「助手」。
        """
        return self._agent_label(self._current_agent_type, self._current_agent_uuid) or "助手"

    def _set_current_agent(self, agent_type: str | None, agent_uuid: str | None) -> None:
        """记录当前正在工作的 agent，供活动行显示。

        Args:
            agent_type: 事件携带的 agent 类型（主 agent 为其 agent_type，如「main」）。
            agent_uuid: 事件携带的 agent 实例 uuid。
        """
        self._current_agent_type = agent_type
        self._current_agent_uuid = agent_uuid

    def on_permission_mode_changed(self) -> None:
        """权限模式变化：重绘状态条以立即反映新模式。"""
        if self._app_running:
            self._app.invalidate()

    def set_permission_mode_toggle_handler(self, handler: Callable[[], None] | None) -> None:
        """登记输入态 Shift+Tab 的权限模式切换回调（None 表示不可用，状态栏据此决定是否提示）。"""
        self._permission_mode_toggle_handler = handler
