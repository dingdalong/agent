"""Shared status-bar controller."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

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
        step_elapsed = self._activity_elapsed(now)
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

        耗时为全会话累计有效耗时（已完成回合的累计 + 本回合实时，均剔除纯人工等待），与会话 token 累计一致。
        处理态与中途弹窗态（回合计时中且非输入态）叠加本回合实时段：有工具在算时走动、纯人工等待时冻结；
        处理态另追加「Ctrl+C 中断」提示。输入态只显示已完成回合的累计值（冻结），不带中断提示。

        Returns:
            可作为 Window 内容的 ANSI（单行核心状态）。
        """
        processing = self._mode == "processing" and bool(self._activity)  # 仅用于「Ctrl+C 中断」提示
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
            # 记录本步起始时的累计暂停基线，本步耗时只剔除其后的暂停；
            # 弹窗期间消费者阻塞、不会触发活动切换，故此刻必不在暂停中。
            self._activity_paused_baseline = self._turn_clock.paused_seconds(now)

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
