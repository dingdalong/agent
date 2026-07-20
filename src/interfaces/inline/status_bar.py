"""Shared status-bar controller."""

from __future__ import annotations

import time
from collections.abc import Callable

from prompt_toolkit.formatted_text import ANSI

from rich.text import Text

from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.status_presenter import present_session_metrics


class StatusBarController:
    """Present permission mode and Store-backed session metrics."""

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

    def present(self, elapsed_seconds: float, toggle_available: bool) -> Text:
        """Build the core status line.

        Args:
            elapsed_seconds: Current or last-turn elapsed time.
            toggle_available: Whether to show the Shift+Tab hint.

        Returns:
            Rich status text.
        """
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


class StatusBarActions:
    """Render and update activity and core status state."""

    def _render_activity(self) -> ANSI:
        """构建活动行「spinner + 当前 agent · 活动 (本步耗时)」的 ANSI；仅处理态且有活动时由其窗口显示。

        首行留空，与上方滚动正文（messages 区）分隔。

        Returns:
            可作为 Window 内容的 ANSI（空行 + 单行活动文案）。
        """
        now = time.monotonic()
        frame = _SPINNER_FRAMES[int(now * 10) % len(_SPINNER_FRAMES)]
        step_elapsed = self._elapsed(self._activity_started_monotonic, now)
        status = Text("\n")
        status.append(f"{frame} ", style="cyan")
        status.append(f"{self._active_agent_name()} · {self._activity} ({step_elapsed:.1f}s)", style="cyan")
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _render_separator(self) -> ANSI:
        """构建一行占满终端宽度的暗色分割线 ANSI（框住输入框，其上、下各一条）。

        Returns:
            可作为 Window 内容的 ANSI（单行分割线）。
        """
        separator = Text("─" * self._render_width, style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(separator, end="")
        return ANSI(capture.get())

    def _render_core_status(self) -> ANSI:
        """构建底部核心状态行的 ANSI：「<权限模式> · ↑输入 ↓输出 · 上下文 XXk(N%) · 耗时 [· 操作提示]」。

        处理态（有活动）显示本回合实时累计耗时并追加「Ctrl+C 中断」提示；
        其余（可输入态、或提交后首个处理事件前的空闲）显示上一回合最终耗时、不带中断提示。

        Returns:
            可作为 Window 内容的 ANSI（单行核心状态）。
        """
        processing = self._mode == "processing" and bool(self._activity)
        elapsed = self._elapsed(self._turn_started_monotonic, time.monotonic()) if processing else self._last_elapsed
        status = Text()
        self._append_core_status(status, elapsed)
        if processing:
            status.append("  ·  ", style="bright_black")
            status.append("Ctrl+C 中断", style="bright_black")
        if self._has_sub_agents():
            status.append("  ·  ", style="bright_black")
            status.append("↓查看 agent", style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _append_core_status(self, line: Text, elapsed: float) -> None:
        """把核心状态段「<权限模式> (Shift+Tab 切换) · ↑总输入 (缓存命中%) ↓输出 · 上下文 XXk(N%) · <耗时>s」原地追加。

        Args:
            line: 目标 Rich Text，原地追加内容。
            elapsed: 要显示的耗时秒数。处理态传本回合实时累计耗时，可输入态传上一回合的最终耗时。
        """
        line.append_text(self._status_bar.present(
            elapsed,
            self._permission_mode_toggle_handler is not None,
        ))

    @staticmethod
    def _elapsed(start: float | None, now: float) -> float:
        """计算从 start 到 now 的耗时秒数；start 为 None（未开始计时）时返回 0.0。

        Args:
            start: 起点 monotonic 秒，None 表示尚未开始。
            now: 当前 monotonic 秒。
        Returns:
            耗时秒数（start 为 None 时为 0.0）。
        """
        return now - start if start is not None else 0.0

    def _set_activity(self, activity: str) -> None:
        """进入处理态并设置当前活动文案，驱动底部状态条 spinner / 活动显示。

        首次记录本回合处理起点；活动切换时重置本步耗时起点。

        Args:
            activity: 当前活动文案（如"思考中"、"回应中"、工具名；空串为提交后的空闲态）。
        """
        activity_changed = activity != self._activity
        self._activity = activity
        self._mode = "processing"
        now = time.monotonic()
        if self._turn_started_monotonic is None:
            self._turn_started_monotonic = now
        if activity_changed:
            self._activity_started_monotonic = now  # 活动切换时重置本步耗时起点

    def _reset_turn_status(self) -> None:
        """清零单回合状态：整轮/本步耗时起点、活动文案与当前 agent。在每次进入输入阶段时调用。

        token 统计由 AgentViewStore 按会话累计，并由 /clear 的 Store reset 清零，不属于单回合状态。
        """
        self._turn_started_monotonic = None
        self._activity_started_monotonic = None
        self._activity = ""
        self._current_agent_type = None
        self._current_agent_uuid = None

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
