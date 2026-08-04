"""生产 Textual TUI 应用。"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from src.events.menu import InputMenu, MenuRequest, PermissionMenu
from src.events.types import (
    CompactDelta,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    PermissionNotice,
    ResponseDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.status_presenter import (
    format_elapsed_time,
    present_agent,
    present_agent_identity,
    present_ended_agent,
    present_session_metrics,
)
from src.interfaces.turn_clock import TurnClock
from src.tools.display import tool_title
from src.interfaces.tui.diagnostics import TuiDiagnostics
from src.interfaces.tui.dialogs import InteractionCoordinator
from src.interfaces.tui.history_journal import PlainHistoryJournal
from src.interfaces.tui.widgets import (

    AgentList,
    Composer,
    HistoryPanel,
    KeyboardListItem,
    NativeClipboard,
    SelectionScreen,
    TranscriptPanel,
)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ROUND_PANEL_MAX_ROWS = 8


@dataclass(slots=True)
class RoundEntry:
    tool_call_id: str
    tool_name: str
    detail: str
    started_monotonic: float
    status: str = "running"
    preview: str = ""
    duration: float = 0.0
    start_display: object | None = None   # ToolDisplay
    result_display: object | None = None  # ToolDisplay


@dataclass(slots=True)
class TranscriptPosition:
    scroll_y: float = 0.0
    follow: bool = True


@dataclass(slots=True)
class TranscriptRenderRequest:
    uuid: str
    version: int
    force: bool
    restore_focus: bool
    invoked: bool
    signature: tuple[Any, ...]
    completed: asyncio.Future[bool] | None = None


class UiCall(Message):
    """把框架事件顺序投递到 Textual 消息循环。"""

    def __init__(
        self,
        callback: Callable[[], Any],
        future: asyncio.Future[Any],
    ) -> None:
        super().__init__()
        self.callback = callback
        self.future = future


class AgentTuiApp(App[None]):
    """生产全屏 TUI。"""

    TITLE = "Agent"
    CSS_PATH = "agent.tcss"
    ALLOW_SELECT = True
    ENABLE_SELECT_AUTO_SCROLL = True
    SELECT_AUTO_SCROLL_LINES = 3
    SELECT_AUTO_SCROLL_SPEED = 60.0
    HORIZONTAL_BREAKPOINTS = [(0, "compact"), (96, "wide")]
    VERTICAL_BREAKPOINTS = [(0, "short"), (32, "tall")]

    BINDINGS = [
        Binding("ctrl+c", "ctrl_c", show=False, priority=True),
        Binding("ctrl+d", "ctrl_d", show=False, priority=True),
        Binding("super+c", "copy_selection", show=False, priority=True),
        Binding("shift+tab", "toggle_plan", show=False, priority=True),
        Binding("ctrl+l", "clear_selection", show=False, priority=True),
    ]

    def __init__(
        self,
        agent_view_store: AgentViewStore,
        slash_commands: list[tuple[str, str]],
        turn_clock: TurnClock,
        request_interrupt: Callable[[], None],
        get_plan_state: Callable[[], bool],
        toggle_plan: Callable[[], None],
        *,
        copy_on_select: bool | None = None,
        platform_name: str | None = None,
        native_clipboard: bool = True,
        history_journal: PlainHistoryJournal | None = None,
        diagnostics: TuiDiagnostics | None = None,
    ) -> None:
        super().__init__()
        self.agent_view_store = agent_view_store
        self.slash_commands = slash_commands
        self.turn_clock = turn_clock
        self.request_interrupt = request_interrupt
        self.get_plan_state = get_plan_state
        self.toggle_plan = toggle_plan
        self.target_platform = platform_name or sys.platform
        self.copy_on_select = (
            self.target_platform == "darwin"
            if copy_on_select is None
            else copy_on_select
        )
        self.native_clipboard_enabled = native_clipboard
        self._native_clipboard = NativeClipboard(self.target_platform)
        self.history_journal = history_journal or PlainHistoryJournal()
        self.diagnostics = diagnostics or TuiDiagnostics(None)
        self.fatal_error: BaseException | None = None
        self.ready = asyncio.Event()
        self.coordinator = InteractionCoordinator(self, turn_clock)

        self._activity = ""
        self._current_agent_type: str | None = None
        self._current_agent_uuid: str | None = None
        self._turn_started: float | None = None
        self._activity_started: float | None = None
        self._activity_pause_baseline = 0.0
        self._session_elapsed = 0.0
        self._retry_deadline: float | None = None
        self._retry_error_kind = ""
        self._retry_safe_message = ""
        self._retry_attempt = 0
        self._retry_max = 0
        self._round_entries: list[RoundEntry] = []
        self._round_agent_type: str | None = None
        self._round_agent_uuid: str | None = None
        self._response_stream: Any = None
        self._thinking_stream: Any = None

        self._completion_matches: list[tuple[str, str]] = []
        self._completion_index = 0
        self._agent_ids: list[str] = []
        self._agent_signature: tuple[str, ...] = ()
        self._main_focus_target = "composer"
        self.viewing_agent_id: str | None = None
        self._transcript_ids: list[str] = []
        self._transcript_index = 0
        self._transcript_positions: dict[str, TranscriptPosition] = {}
        self._transcript_signature: tuple[Any, ...] | None = None
        self._transcript_generation = 0
        self._rendered_transcript_id: str | None = None
        self._transcript_requested_signature: tuple[Any, ...] | None = None
        self._transcript_pending: TranscriptRenderRequest | None = None
        self._transcript_render_event = asyncio.Event()
        self._transcript_worker_task: asyncio.Task[None] | None = None
        self._transcript_merged_requests = 0
        self._transcript_active_renders = 0
        self._transcript_max_concurrent_renders = 0
        self._clipboard_pending: str | None = None
        self._clipboard_worker_task: asyncio.Task[None] | None = None

    def _handle_exception(self, error: Exception) -> None:
        """保留 Textual 吞掉的 fatal exception，供外层生命周期检测。"""
        self.fatal_error = error
        self.diagnostics.record_exception(
            "textual_fatal_error",
            error,
        )
        super()._handle_exception(error)

    @property
    def completion_visible(self) -> bool:
        return bool(self._completion_matches)

    def compose(self) -> ComposeResult:
        yield HistoryPanel(id="history")
        with Vertical(id="transient-zone"):
            yield Static("", id="activity", markup=False)
            with Vertical(id="transcript-zone"):
                yield Static("", id="transcript-header", markup=False)
                with TranscriptPanel(id="transcript-panel"):
                    yield Markdown("", id="transcript-content")
            yield Static("", id="completion", markup=False)
        yield Static("", classes="separator", id="separator-top", markup=False)
        with Vertical(id="composer-shell"):
            yield Composer(
                "",
                id="input",
                soft_wrap=True,
                show_line_numbers=False,
                placeholder="输入消息或 / 命令…",
            )
        yield Static("", classes="separator", id="separator-bottom", markup=False)
        yield Static("", id="core-status", markup=False)
        yield AgentList(id="agent-list", initial_index=0)

    def get_default_screen(self) -> SelectionScreen:
        return SelectionScreen(id="_default")

    async def on_mount(self) -> None:
        self._history = self.query_one("#history", HistoryPanel)
        self._activity_widget = self.query_one("#activity", Static)
        self._transcript_zone = self.query_one("#transcript-zone", Vertical)
        self._transcript_header = self.query_one("#transcript-header", Static)
        self._transcript_panel = self.query_one("#transcript-panel", TranscriptPanel)
        self._transcript_content = self.query_one("#transcript-content", Markdown)
        self._completion_widget = self.query_one("#completion", Static)
        self._composer_shell = self.query_one("#composer-shell", Vertical)
        self._composer = self.query_one("#input", Composer)
        self._status = self.query_one("#core-status", Static)
        self._agent_list = self.query_one("#agent-list", AgentList)
        self._separator_top = self.query_one("#separator-top", Static)
        self._separator_bottom = self.query_one("#separator-bottom", Static)
        self._composer.read_only = True
        self._composer.show_cursor = False
        self._transcript_worker_task = asyncio.create_task(
            self._transcript_render_worker(),
            name="tui-transcript-render",
        )
        self._resize_composer()
        self._update_separators()
        self._tick_timer = self.set_interval(0.1, self._tick)
        self.refresh_chrome()
        self.ready.set()
        self.diagnostics.record(
            "app_mounted",
        )

    async def on_unmount(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        await self._stop_transcript_worker()
        if self._clipboard_worker_task is not None:
            await self._clipboard_worker_task

    async def invoke(self, callback: Callable[[], Any]) -> Any:
        future = asyncio.get_running_loop().create_future()
        if not self.post_message(UiCall(callback, future)):
            raise RuntimeError("Textual app is not accepting messages")
        return await future

    async def on_ui_call(self, message: UiCall) -> None:
        if message.future.done():
            return
        try:
            result = message.callback()
            if inspect.isawaitable(result):
                result = await result
            message.future.set_result(result)
        except BaseException as exc:
            message.future.set_exception(exc)

    async def shutdown_ui(self) -> None:
        await self.coordinator.close()
        await self.end_response()
        await self.end_thinking()
        await self._stop_transcript_worker()
        self.exit()

    async def _stop_transcript_worker(self) -> None:
        task = self._transcript_worker_task
        self._transcript_worker_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def set_screen_class(self, add: bool, class_name: str) -> None:
        self.screen.set_class(add, class_name)

    def _tick(self) -> None:
        if not self._screen_stack:
            return
        self._update_separators()
        self._schedule_agent_refresh()
        self._schedule_transcript_refresh()
        self.refresh_chrome()

    def _update_separators(self) -> None:
        if not hasattr(self, "_separator_top"):
            return
        separator = "─" * max(1, self.screen.size.width)
        self._separator_top.update(separator)
        self._separator_bottom.update(separator)

    async def append_output(
        self,
        message: str | Text,
        markdown: bool = False,
        *,
        classes: str = "history-static",
    ) -> None:
        if markdown and isinstance(message, str):
            await self._history.mount(Markdown(message, classes=classes or None))
        else:
            await self._history.mount(
                Static(message, classes=classes or None, markup=False)
            )
        self.history_journal.append_entry(message)

    async def append_markdown(
        self,
        source: str,
        classes: str = "",
        *,
        stream_id: str | None = None,
    ) -> Markdown:
        widget = Markdown(source, classes=classes or None)
        await self._history.mount(widget)
        if stream_id is None:
            self.history_journal.append_entry(source)
        else:
            self.history_journal.start_stream(stream_id, source)
        return widget

    async def append_user(self, text: str) -> None:
        lines = text.split("\n")
        rendered = "\n".join(
            f"› {line}" if index == 0 else f"  {line}"
            for index, line in enumerate(lines)
        )
        await self._history.mount(
            Static(rendered, classes="user-message", markup=False)
        )
        self.history_journal.append_entry(rendered)

    async def record_request_context(self, request: MenuRequest) -> None:
        if isinstance(request, InputMenu):
            return
        label = self._agent_label(request.caller_agent_type, request.caller_uuid)
        if label:
            banner = Text("\n› ", style="bold cyan")
            banner.append(label, style="bold cyan")
            await self.append_output(banner)
        if isinstance(request, PermissionMenu):
            text = Text("工具请求权限\n", style="bold yellow")
            text.append(f"工具: {request.tool_name}\n")
            text.append(f"内容: {request.detail}")
            if request.reason:
                text.append(
                    f"\n智能权限 · {tool_title(request.tool_name)} · 需确认({request.reason[:60]})",
                    style="#efc36a",
                )
            await self.append_output(text)
            return
        prompt = getattr(request, "prompt", "")
        if prompt.strip():
            await self.append_output(prompt, markdown=getattr(request, "markdown", False))

    async def begin_input(self, request: InputMenu) -> None:
        await self.flush_round()
        self._session_elapsed += self._turn_elapsed(time.monotonic())
        self._main_focus_target = "composer"
        lines = request.prompt.splitlines()
        context = "\n".join(lines[:-1]).strip("\n") if len(lines) > 1 else ""
        if context.strip():
            await self.append_output(context, markdown=request.markdown)
        self._composer.load_text(request.default)
        if request.default:
            split = request.default.split("\n")
            self._composer.move_cursor((len(split) - 1, len(split[-1])))
        self._resize_composer()
        self.sync_input_state()
        self.call_after_refresh(self.restore_focus)

    async def finish_input(self, text: str) -> None:
        await self.append_user(text)
        self._composer.clear()
        self._resize_composer()
        self.hide_completion()
        self._reset_turn_status()
        self.sync_input_state()

    def finish_input_cancelled(self) -> None:
        self._composer.clear()
        self._resize_composer()
        self.hide_completion()
        self._reset_turn_status()
        self.sync_input_state()

    def sync_input_state(self) -> None:
        if not hasattr(self, "_composer"):
            return
        input_visible = (
            not self.viewing_agent_id
            and not self.coordinator.modal_active
        )
        editable = (
            self.coordinator.input_active
            and input_visible
        )
        self._composer.read_only = not editable
        self._composer.show_cursor = input_visible

    def focus_composer(self) -> None:
        self._main_focus_target = "composer"
        if hasattr(self, "_composer"):
            self._composer.focus()

    def on_app_focus(self, _event: events.AppFocus) -> None:
        """终端窗口重新激活后，把键盘交还给当前交互面板。"""
        self.call_after_refresh(self.restore_focus)

    def restore_focus(self) -> None:
        self.sync_input_state()
        if self.coordinator.modal is not None:
            if self.coordinator.modal.is_mounted:
                self.coordinator.modal.restore_focus()
            return
        if self.viewing_agent_id:
            self._transcript_panel.focus()
        elif (
            self._main_focus_target == "agent_list"
            and self._has_sub_agents()
        ):
            self._agent_list.focus()
        else:
            self.focus_composer()

    def refresh_chrome(self) -> None:
        if not hasattr(self, "_composer"):
            return
        self._render_activity()
        self._render_status()
        self._render_completion()
        self.sync_input_state()
        self._sync_agent_list_visibility()

    def _render_activity(self) -> None:
        visible = (
            not self.coordinator.input_active
            and not self.viewing_agent_id
            and not self.coordinator.modal_active
            and bool(self._activity or self._round_entries or self._retry_deadline is not None)
        )
        self._activity_widget.display = visible
        if not visible:
            return
        now = time.monotonic()
        frame = _SPINNER[int(now * 10) % len(_SPINNER)]
        if self._retry_deadline is not None:
            remaining = max(0, math.ceil(self._retry_deadline - now))
            self._activity_widget.update(
                f"{frame} {self._active_agent_name()} · "
                f"LLM错误[{self._retry_error_kind}] {self._retry_safe_message}，"
                f"{remaining}秒后重试 ({self._retry_attempt}/{self._retry_max})"
            )
            return
        if self._round_entries:
            done = sum(entry.status != "running" for entry in self._round_entries)
            lines = [
                f"{frame} 本轮 · {len(self._round_entries)} 工具"
                f"（{done} 完成 · {len(self._round_entries) - done} 进行中）"
            ]
            for entry in self._round_entries[:_ROUND_PANEL_MAX_ROWS]:
                if entry.status == "running":
                    mark = frame
                    elapsed = max(0.0, now - entry.started_monotonic)
                else:
                    mark = "✔" if entry.status == "success" else "✘"
                    elapsed = entry.duration
                detail = f" {entry.detail}" if entry.detail else ""
                lines.append(
                    f"  {mark} {entry.tool_name}{detail} ({format_elapsed_time(elapsed)})"
                )
            hidden = len(self._round_entries) - _ROUND_PANEL_MAX_ROWS
            if hidden > 0:
                lines.append(f"  … 还有 {hidden} 个")
            self._activity_widget.update("\n".join(lines))
            return
        self._activity_widget.update(
            f"{frame} {self._active_agent_name()} · {self._activity} "
            f"({format_elapsed_time(self._activity_elapsed(now))})"
        )

    def _render_status(self) -> None:
        now = time.monotonic()
        if self.viewing_agent_id:
            snapshot = self.agent_view_store.agent_snapshot(self.viewing_agent_id)
            status = (
                present_agent(snapshot)
                if snapshot is not None
                else present_ended_agent(self.viewing_agent_id)
            )
        else:
            if self._turn_started is not None and not self.coordinator.input_active:
                elapsed = self._session_elapsed + self._turn_elapsed(now)
            else:
                elapsed = self._session_elapsed
            status = Text("plan" if self.get_plan_state() else "normal")
            status.append(" (Shift+Tab 切换)", style="bright_black")
            status.append("  ·  ", style="bright_black")
            status.append_text(
                present_session_metrics(
                    self.agent_view_store.session_snapshot(),
                    elapsed,
                )
            )
            processing = (
                not self.coordinator.input_active
                and not self.coordinator.modal_active
                and bool(self._activity or self._retry_deadline is not None)
            )
            if processing:
                status.append("  ·  Ctrl+C 中断", style="bright_black")
            elif self.coordinator.input_active and self._has_sub_agents():
                status.append("  ·  ↓查看 agent", style="bright_black")
        pending_count, pending_source = self.coordinator.pending_summary
        if pending_count:
            status.append("  ·  ", style="bright_black")
            status.append(f"等待 {pending_count}：{pending_source}", style="yellow")
        self._status.update(status)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if not hasattr(self, "_composer") or event.text_area is not self._composer:
            return
        self._resize_composer()
        self._update_completions(self._composer.text)

    def _resize_composer(self) -> None:
        if not hasattr(self, "_composer"):
            return
        line_count = min(8, max(1, self._composer.text.count("\n") + 1))
        self._composer_shell.styles.height = line_count

    def _update_completions(self, text: str) -> None:
        stripped = text.lstrip()
        if (
            not stripped.startswith("/")
            or " " in stripped
            or not self.coordinator.input_active
            or self.viewing_agent_id
            or self.coordinator.modal_active
        ):
            self.hide_completion()
            return
        prefix = stripped[1:].lower()
        self._completion_matches = [
            command for command in self.slash_commands if command[0].startswith(prefix)
        ]
        self._completion_index = min(
            self._completion_index,
            max(0, len(self._completion_matches) - 1),
        )
        self._render_completion()

    def _render_completion(self) -> None:
        self._completion_widget.display = self.completion_visible
        if not self.completion_visible:
            return
        lines = []
        for index, (name, description) in enumerate(self._completion_matches[:8]):
            marker = "❯" if index == self._completion_index else " "
            lines.append(f"{marker} /{name}  —  {description}")
        self._completion_widget.update("\n".join(lines))

    def hide_completion(self) -> None:
        self._completion_matches = []
        self._completion_index = 0
        if hasattr(self, "_completion_widget"):
            self._render_completion()

    def select_next_completion(self) -> bool:
        if not self.completion_visible:
            return False
        self._completion_index = (self._completion_index + 1) % len(self._completion_matches)
        self._render_completion()
        return True

    def select_previous_completion(self) -> bool:
        if not self.completion_visible:
            return False
        self._completion_index = (self._completion_index - 1) % len(self._completion_matches)
        self._render_completion()
        return True

    def apply_completion(self) -> bool:
        if not self.completion_visible:
            return False
        command = self._completion_matches[self._completion_index][0]
        value = f"/{command} "
        self._composer.load_text(value)
        self._composer.move_cursor((0, len(value)))
        self.hide_completion()
        return True

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        if self._composer.read_only or not event.text.strip():
            return
        await self.coordinator.complete_input(event.text)

    def _schedule_agent_refresh(self) -> None:
        snapshots = self._browser_snapshots()
        signature = tuple(
            f"{snapshot.uuid}:{present_agent(snapshot).plain}"
            for snapshot in snapshots
        )
        if signature == self._agent_signature:
            return
        self._agent_signature = signature
        self._run_presentation_worker(
            lambda: self._sync_agent_list(snapshots),
            group="agent-list",
        )

    def _run_presentation_worker(
        self,
        work_factory: Callable[[], Awaitable[Any]],
        *,
        group: str,
    ) -> None:
        """运行不会因展示异常而关闭整个 Textual 应用的后台任务。"""
        self.run_worker(
            self._guard_presentation_worker(work_factory, group),
            group=group,
            exclusive=True,
            exit_on_error=False,
        )

    async def _guard_presentation_worker(
        self,
        work_factory: Callable[[], Awaitable[Any]],
        group: str,
    ) -> None:
        try:
            await work_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error("TUI presentation worker failed (%s): %s", group, exc)
            self.diagnostics.record_exception(
                "presentation_worker_failed",
                exc,
                worker_group=group,
            )

    async def _sync_agent_list(self, snapshots) -> None:
        ids = [snapshot.uuid for snapshot in snapshots]
        if ids != self._agent_ids:
            selected_uuid = (
                self._agent_ids[self._agent_list.index]
                if self._agent_list.index is not None
                and self._agent_list.index < len(self._agent_ids)
                else None
            )
            await self._agent_list.remove_children()
            for snapshot in snapshots:
                label = snapshot.agent_type if snapshot.is_main else present_agent(snapshot)
                await self._agent_list.mount(
                    KeyboardListItem(Static(label, markup=False))
                )
            self._agent_ids = ids
            if ids:
                self._agent_list.index = ids.index(selected_uuid) if selected_uuid in ids else 0
        else:
            for item, snapshot in zip(self._agent_list.children, snapshots):
                label = next(iter(item.query(Static)), None)
                if label is not None:
                    label.update(snapshot.agent_type if snapshot.is_main else present_agent(snapshot))
        self._sync_agent_list_visibility()

    def _sync_agent_list_visibility(self) -> None:
        if not hasattr(self, "_agent_list"):
            return
        has_sub_agents = self._has_sub_agents()
        self._agent_list.display = (
            has_sub_agents
            and not self.viewing_agent_id
            and not self.coordinator.modal_active
        )
        if (
            not has_sub_agents
            and not self.viewing_agent_id
            and self._main_focus_target == "agent_list"
        ):
            self.focus_composer()

    def _has_sub_agents(self) -> bool:
        return bool(self.agent_view_store.subagent_snapshots())

    def _browser_snapshots(self):
        main = [
            snapshot
            for snapshot in self.agent_view_store.active_agent_snapshots()
            if snapshot.is_main
        ]
        return main + self.agent_view_store.subagent_snapshots()

    def focus_agent_list(self) -> bool:
        if (
            self.viewing_agent_id
            or self.coordinator.modal_active
            or not self._has_sub_agents()
        ):
            return False
        self._agent_list.index = max(0, self._agent_list.index or 0)
        self._main_focus_target = "agent_list"
        self._agent_list.focus()
        return True

    def on_list_view_selected(self, event: AgentList.Selected) -> None:
        index = event.list_view.index
        snapshots = self._browser_snapshots()
        if index is None or index >= len(snapshots) or snapshots[index].is_main:
            self.focus_composer()
            return
        self._run_presentation_worker(
            lambda: self.coordinator.open_live_transcript(snapshots[index].uuid),
            group="transcript-open",
        )

    async def open_transcript(
        self,
        uuid: str,
        source_ids: list[str],
        *,
        invoked: bool,
    ) -> bool:
        if self.agent_view_store.agent_snapshot(uuid) is None:
            return False
        self._set_transcript_target(uuid, source_ids, invoked=invoked)
        await self._request_transcript_render(
            force=True,
            restore_focus=True,
            invoked=invoked,
        )
        return True

    def _set_transcript_target(
        self,
        uuid: str,
        source_ids: list[str],
        *,
        invoked: bool,
    ) -> None:
        if invoked:
            self._main_focus_target = "composer"
        self._save_transcript_position()
        ids = [
            item
            for item in source_ids
            if self.agent_view_store.agent_snapshot(item) is not None
        ]
        if uuid not in ids:
            ids.append(uuid)
        self.viewing_agent_id = uuid
        self._transcript_ids = ids
        self._transcript_index = ids.index(uuid)
        self.set_screen_class(True, "viewing")
        self._transcript_zone.display = True
        self.sync_input_state()
        self.refresh_chrome()

    def _save_transcript_position(self) -> None:
        if self._transcript_active_renders:
            return
        uuid = self._rendered_transcript_id
        if uuid is None or not hasattr(self, "_transcript_panel"):
            return
        position = self._transcript_positions.setdefault(
            uuid,
            TranscriptPosition(),
        )
        position.scroll_y = self._transcript_panel.scroll_y
        position.follow = self._transcript_panel.scroll_y >= self._transcript_panel.max_scroll_y - 1

    def _schedule_transcript_refresh(self) -> None:
        if not self.viewing_agent_id or self.coordinator.modal_active:
            return
        self._save_transcript_position()
        signature = self._current_transcript_signature(self.viewing_agent_id)
        if signature == self._transcript_requested_signature:
            self._update_transcript_header(invoked=self.coordinator.view_request is not None)
            return
        self._queue_transcript_render(
            force=False,
            restore_focus=False,
            invoked=self.coordinator.view_request is not None,
            signature=signature,
        )

    def _current_transcript_signature(self, uuid: str | None = None) -> tuple[Any, ...]:
        uuid = uuid if uuid is not None else self.viewing_agent_id or ""
        messages = self.agent_view_store.transcript_messages(uuid)
        if messages:
            diagnostics = [
                segment
                for segment in self.agent_view_store.transcript_segments(uuid)
                if segment[0] in {"retry", "error"}
            ]
            return (
                uuid,
                "messages",
                len(messages),
                sum(len(str(message)) for message in messages),
                len(diagnostics),
                sum(len(text) for _kind, text in diagnostics),
            )
        segments = self.agent_view_store.transcript_segments(uuid)
        return (
            uuid,
            "segments",
            len(segments),
            sum(len(text) for _kind, text in segments),
        )

    async def _request_transcript_render(
        self,
        *,
        force: bool,
        restore_focus: bool,
        invoked: bool | None = None,
    ) -> bool:
        completed = self._queue_transcript_render(
            force=force,
            restore_focus=restore_focus,
            invoked=(
                self.coordinator.view_request is not None
                if invoked is None
                else invoked
            ),
        )
        return await completed if completed is not None else False

    def _queue_transcript_render(
        self,
        *,
        force: bool,
        restore_focus: bool,
        invoked: bool,
        signature: tuple[Any, ...] | None = None,
    ) -> asyncio.Future[bool] | None:
        uuid = self.viewing_agent_id
        if uuid is None:
            return None
        signature = signature or self._current_transcript_signature(uuid)
        if not force and signature == self._transcript_requested_signature:
            return None
        self._transcript_generation += 1
        version = self._transcript_generation
        completed = asyncio.get_running_loop().create_future()
        request = TranscriptRenderRequest(
            uuid=uuid,
            version=version,
            force=force,
            restore_focus=restore_focus,
            invoked=invoked,
            signature=signature,
            completed=completed,
        )
        previous = self._transcript_pending
        if previous is not None:
            self._transcript_merged_requests += 1
            if previous.completed is not None and not previous.completed.done():
                previous.completed.set_result(False)
        self._transcript_pending = request
        self._transcript_requested_signature = signature
        self._transcript_render_event.set()
        self.diagnostics.record(
            "transcript_render_queued",
            version=version,
            target_uuid=uuid,
            merged_requests=self._transcript_merged_requests,
            response_stream_active=self._response_stream is not None,
            thinking_stream_active=self._thinking_stream is not None,
        )
        return completed

    async def _transcript_render_worker(self) -> None:
        self.diagnostics.record(
            "transcript_worker_started",
        )
        try:
            while True:
                await self._transcript_render_event.wait()
                self._transcript_render_event.clear()
                while self._transcript_pending is not None:
                    request = self._transcript_pending
                    self._transcript_pending = None
                    rendered = False
                    self._transcript_active_renders += 1
                    self._transcript_max_concurrent_renders = max(
                        self._transcript_max_concurrent_renders,
                        self._transcript_active_renders,
                    )
                    try:
                        rendered = await self._render_transcript_request(request)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.log.error("TUI transcript render failed: %s", exc)
                        self.diagnostics.record_exception(
                            "transcript_render_failed",
                            exc,
                            version=request.version,
                            target_uuid=request.uuid,
                        )
                    finally:
                        self._transcript_active_renders -= 1
                        if request.completed is not None and not request.completed.done():
                            request.completed.set_result(rendered)
        except asyncio.CancelledError:
            raise
        finally:
            pending = self._transcript_pending
            if pending is not None and pending.completed is not None and not pending.completed.done():
                pending.completed.set_result(False)
            self.diagnostics.record(
                "transcript_worker_stopped",
            )

    async def _render_transcript_request(
        self,
        request: TranscriptRenderRequest,
    ) -> bool:
        started = time.monotonic()
        if request.version != self._transcript_generation:
            return False
        if not request.force and request.signature == self._transcript_signature:
            return False
        uuid = request.uuid
        position = self._transcript_positions.setdefault(uuid, TranscriptPosition())
        source = self._transcript_source(uuid)
        if request.version != self._transcript_generation:
            return False
        await self._transcript_content.update(source)
        self._rendered_transcript_id = uuid
        previous_geometry: tuple[int, float] | None = None
        for _ in range(4):
            await self._transcript_panel.wait_for_refresh()
            geometry = (
                self._transcript_panel.virtual_size.height,
                self._transcript_panel.max_scroll_y,
            )
            if geometry == previous_geometry:
                break
            previous_geometry = geometry
        current = (
            self.viewing_agent_id == uuid
            and request.version == self._transcript_generation
        )
        if current:
            if position.follow:
                self._transcript_panel.scroll_end(animate=False, force=True, immediate=True)
            else:
                self._transcript_panel.scroll_to(
                    y=position.scroll_y,
                    animate=False,
                    force=True,
                    immediate=True,
                )
            position.scroll_y = self._transcript_panel.scroll_y
            self._transcript_signature = request.signature
            self._update_transcript_header(invoked=request.invoked)
            if request.restore_focus:
                self._transcript_panel.focus()
            self.refresh_chrome()
        self.diagnostics.record(
            "transcript_render_finished",
            version=request.version,
            target_uuid=uuid,
            current=current,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            merged_requests=self._transcript_merged_requests,
        )
        return current

    def _transcript_source(self, uuid: str) -> str:
        messages = self.agent_view_store.transcript_messages(uuid)
        blocks: list[str] = []
        if messages:
            for message in messages:
                blocks.extend(self._message_blocks(message))
            for kind, text in self.agent_view_store.transcript_segments(uuid):
                if kind in {"retry", "error"}:
                    blocks.append(f"### {'⚠ 重试' if kind == 'retry' else '✘ 错误'}\n\n{text}")
            return "\n\n".join(blocks)
        labels = {
            "response": "● 助手",
            "thinking": "◇ 思考",
            "tool": "⚙ 工具",
            "retry": "⚠ 重试",
            "error": "✘ 错误",
        }
        for kind, text in self.agent_view_store.transcript_segments(uuid):
            blocks.append(f"### {labels.get(kind, kind)}\n\n{text}")
        return "\n\n".join(blocks)

    def _message_blocks(self, message: dict) -> list[str]:
        if not isinstance(message, dict):
            return [f"### [消息]\n\n{message!s}"]
        role = message.get("role", "")
        blocks: list[str] = []
        if role == "user":
            blocks.append(f"### ▶ 用户\n\n{message.get('content') or ''}")
        elif role == "assistant":
            thinking = message.get("reasoning_content") or message.get("reasoning")
            if thinking:
                blocks.append(f"### ◇ 思考\n\n{thinking}")
            content = message.get("content")
            if content:
                blocks.append(f"### ● 助手\n\n{content}")
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, (list, tuple)):
                tool_calls = [tool_calls]
            for call in tool_calls:
                if not isinstance(call, dict):
                    blocks.append(f"### ⚙ 工具调用\n\n{call!s}")
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    blocks.append(f"### ⚙ 工具调用\n\n{function!s}")
                    continue
                arguments = self._format_tool_arguments(function.get("arguments", ""))
                blocks.append(
                    f"### ⚙ {function.get('name', '')}\n\n````json\n{arguments}\n````"
                )
        elif role == "tool":
            tail = str(message.get("tool_call_id") or "").split("-")[0]
            blocks.append(
                f"### ⚙ 结果 ({tail})\n\n{message.get('content') or ''}"
            )
        else:
            blocks.append(f"### [{role}]\n\n{message.get('content') or ''}")
        return blocks

    @staticmethod
    def _format_tool_arguments(arguments: Any) -> str:
        if not isinstance(arguments, str):
            try:
                return json.dumps(arguments, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                return str(arguments)
        try:
            return json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            return str(arguments)

    def _update_transcript_header(self, *, invoked: bool) -> None:
        if not self.viewing_agent_id:
            return
        snapshot = self.agent_view_store.agent_snapshot(self.viewing_agent_id)
        label = (
            present_agent_identity(snapshot, show_status=False).plain
            if snapshot is not None
            else present_ended_agent(self.viewing_agent_id).plain
        )
        position = self._transcript_positions.setdefault(
            self.viewing_agent_id,
            TranscriptPosition(),
        )
        distance = max(0, self._transcript_panel.max_scroll_y - position.scroll_y)
        follow = "实时" if position.follow else f"已上滚 {distance:.0f} 行"
        exit_hint = "Esc 返回列表" if invoked else "Esc 关闭"
        self._transcript_header.update(
            f"── {label} ──  {follow}  ·  ↑/↓ 滚动 · ←/→ 切换 · {exit_hint}"
        )

    def switch_transcript(self, delta: int) -> None:
        if self.coordinator.modal_active or not self.viewing_agent_id:
            return
        ids = [
            uuid
            for uuid in self._transcript_ids
            if self.agent_view_store.agent_snapshot(uuid) is not None
        ]
        if self.viewing_agent_id not in ids:
            self.hide_transcript()
            return
        self._transcript_ids = ids
        self._transcript_index = ids.index(self.viewing_agent_id)
        next_index = min(
            max(0, self._transcript_index + delta),
            len(self._transcript_ids) - 1,
        )
        if next_index == self._transcript_index:
            return
        previous_index = self._transcript_index
        uuid = self._transcript_ids[next_index]
        invoked = self.coordinator.view_request is not None
        self._set_transcript_target(uuid, self._transcript_ids, invoked=invoked)
        self._queue_transcript_render(
            force=True,
            restore_focus=True,
            invoked=invoked,
        )
        self.diagnostics.record(
            "transcript_switched",
            version=self._transcript_generation,
            previous_index=previous_index,
            target_index=next_index,
            target_uuid=uuid,
        )

    def close_transcript(self) -> None:
        if not self.coordinator.modal_active:
            self.coordinator.close_transcript()

    def hide_transcript(self) -> None:
        self._save_transcript_position()
        self._transcript_generation += 1
        pending = self._transcript_pending
        self._transcript_pending = None
        if pending is not None and pending.completed is not None and not pending.completed.done():
            pending.completed.set_result(False)
        self.viewing_agent_id = None
        self._transcript_ids = []
        self._transcript_signature = None
        self._transcript_requested_signature = None
        self._transcript_zone.display = False
        self.set_screen_class(False, "viewing")
        self.sync_input_state()
        self.refresh_chrome()
        self.diagnostics.record(
            "transcript_hidden",
            version=self._transcript_generation,
        )

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("回应中")
        if self._response_stream is None:
            await self.append_output(
                f"› {self._response_prefix(event)}",
                classes="stream-label",
            )
            widget = await self.append_markdown("", stream_id="response")
            self._response_stream = Markdown.get_stream(widget)
        await self._response_stream.write(content)
        self.history_journal.append_stream("response", content)

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("思考中")
        if self._thinking_stream is None:
            await self.append_output(
                f"› {self._thinking_prefix(event)}",
                classes="stream-label",
            )
            widget = await self.append_markdown(
                "",
                classes="thinking-output",
                stream_id="thinking",
            )
            self._thinking_stream = Markdown.get_stream(widget)
        await self._thinking_stream.write(content)
        self.history_journal.append_stream("thinking", content)

    async def end_response(self) -> None:
        if self._response_stream is not None:
            await self._response_stream.stop()
            self._response_stream = None
        self.history_journal.end_stream("response")

    async def end_thinking(self) -> None:
        if self._thinking_stream is not None:
            await self._thinking_stream.stop()
            self._thinking_stream = None
        self.history_journal.end_stream("thinking")

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        await self.flush_round()
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        activity = (
            f"等待响应 {event.attempt}/{event.max_attempts}"
            if event.attempt > 1 and event.max_attempts > 0
            else "等待响应"
        )
        self._set_activity(activity)

    async def on_llm_retrying(self, event: LLMRetrying) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        if event.partial:
            await self.append_output(
                Text(
                    f"⚠ 尝试 {event.attempt}/{event.max_attempts} 失败，将重试 "
                    f"[{event.error_kind}] {event.safe_message} "
                    f"(partial=true, tool={event.tool_fragment_state})",
                    style="yellow",
                )
            )
        now = time.monotonic()
        self._retry_deadline = now + event.wait_seconds
        self._retry_error_kind = event.error_kind
        self._retry_safe_message = event.safe_message
        self._retry_attempt = event.attempt
        self._retry_max = event.max_attempts
        if self._turn_started is None:
            self._turn_started = now

    async def on_llm_length_retrying(self, event: LLMLengthRetrying) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        labels = {
            "tool_call": "工具调用",
            "content": "正文",
            "thinking": "思考",
            "unknown": "未知",
        }
        kind = labels.get(event.truncation_kind, event.truncation_kind)
        if event.strategy == "regenerate-lower-effort":
            action = f"降低推理力度至 {event.effort} 后重生成"
        elif event.strategy == "regenerate-compress":
            action = "压缩思考后重生成"
        else:
            action = "从中断处继续生成"
        await self.append_output(
            Text(
                f"⚠ 输出截断（{kind}）：{action} ({event.attempt}/{event.max_attempts})",
                style="yellow",
            )
        )

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("失败")
        identifiers = []
        if event.request_id:
            identifiers.append(f"request_id={event.request_id}")
        if event.diagnostic_id:
            identifiers.append(f"diagnostic_id={event.diagnostic_id}")
        suffix = f" ({', '.join(identifiers)})" if identifiers else ""
        await self.append_output(
            Text(
                f"✘ LLM 调用失败 [{event.error_kind}] {event.safe_message}{suffix}",
                style="red",
            )
        )

    async def on_compact_delta(self, event: CompactDelta) -> None:
        self._set_activity("压缩上下文")
        label = self._agent_label(event.caller_agent_type, event.caller_uuid)
        prefix = f"{label} " if label else ""
        await self.append_output(
            Text(f"[compact] {prefix}{event.content.strip() or 'context'}", style="bright_black")
        )

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        title = tool_title(event.tool_name)
        reason = (event.detail or "")[:60]
        if event.status == "deny":
            await self.append_output(
                Text(f"智能权限 · {title} · 已拒绝({reason})", style="#ff6b6b")
            )
        elif event.status == "allow":
            await self.append_output(
                Text(f"智能权限 · {title} · 已放行({reason})", style="#efc36a")
            )

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity(event.tool_name)
        if event.caller_uuid != self.agent_view_store.foreground_uuid:
            return
        if not self._round_entries:
            self._round_agent_type = event.caller_agent_type
            self._round_agent_uuid = event.caller_uuid
        self._round_entries.append(
            RoundEntry(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                detail=event.detail.strip(),
                started_monotonic=time.monotonic(),
                start_display=event.display,
            )
        )

    async def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        for entry in self._round_entries:
            if entry.tool_call_id == event.tool_call_id:
                entry.status = "success" if event.status == "success" else "error"
                entry.preview = (event.result_preview or "").strip()
                entry.duration = event.duration_seconds
                entry.result_display = event.display
                break

    async def flush_round(self) -> None:
        if not self._round_entries:
            return
        for entry in self._round_entries:
            text = Text()
            if entry.status == "running":
                # 中断态
                title = self._entry_title(entry)
                text.append(f"⋯ {title}", style="bright_black")
                text.append("  已中断", style="bright_black")
                await self.append_output(text)
                continue
            ok = entry.status == "success"
            mark = "✔" if ok else "✘"
            title = self._entry_title(entry)
            title_style = "green" if ok else "red"

            # 标题行：{mark} {title}  右对齐耗时
            text.append(f"{mark} {title}", style=title_style)
            text.append(f"  ({entry.duration:.2f}s)", style="bright_black")

            # 参数内容（来自 start_display）
            start_content = self._display_content(entry.start_display)
            if start_content:
                text.append("\n")
                for line in start_content.splitlines():
                    text.append(f"  {line}\n", style="bright_black")

            # 结果内容
            result_display = entry.result_display
            if result_display is not None and hasattr(result_display, "content_type"):
                if result_display.content_type == "diff" and result_display.content:
                    text.append("  ─────\n", style="bright_black")
                    for line in result_display.content.splitlines():
                        stripped = line.lstrip()
                        if stripped.startswith("+ "):
                            text.append(f"{line}\n", style="green on #1a2e1a")
                        elif stripped.startswith("- "):
                            text.append(f"{line}\n", style="red on #2e1a1a")
                        else:
                            text.append(f"{line}\n", style="bright_black")
                    if result_display.truncated:
                        text.append("  … (已截断)\n", style="bright_black")
                elif result_display.content:
                    text.append("  ─────\n", style="bright_black")
                    for line in result_display.content.splitlines()[:20]:
                        text.append(f"  {line}\n", style="bright_black" if ok else "red")
                    if result_display.truncated:
                        text.append("  … (已截断)\n", style="bright_black")
            elif not ok:
                # 降级：无 display 时用 preview
                preview_lines = entry.preview.splitlines()
                if preview_lines:
                    text.append("\n")
                    for line in preview_lines[:5]:
                        text.append(f"  {line}\n", style="red")

            await self.append_output(text)
        self._round_entries = []
        self._round_agent_type = None
        self._round_agent_uuid = None

    @staticmethod
    def _entry_title(entry: RoundEntry) -> str:
        """返回 RoundEntry 的展示标题。"""
        for disp in (entry.result_display, entry.start_display):
            if disp is not None:
                title = getattr(disp, "title", "") or ""
                if title:
                    return title
        detail = f" {entry.detail}" if entry.detail else ""
        return f"{entry.tool_name}{detail}"

    @staticmethod
    def _display_content(display: object | None) -> str:
        """提取 ToolDisplay 的内容文本。"""
        if display is None or not hasattr(display, "content"):
            return ""
        return (display.content or "").strip()

    def _set_activity(self, activity: str) -> None:
        left_retry = self._retry_deadline is not None
        self._clear_retry()
        changed = activity != self._activity
        self._activity = activity
        now = time.monotonic()
        if self._turn_started is None:
            self._turn_started = now
        if changed or left_retry:
            self._activity_started = now
            self._activity_pause_baseline = self.turn_clock.paused_seconds(now)
        self.refresh_chrome()

    def _clear_retry(self) -> None:
        self._retry_deadline = None
        self._retry_error_kind = ""
        self._retry_safe_message = ""
        self._retry_attempt = 0
        self._retry_max = 0

    def _reset_turn_status(self) -> None:
        self._turn_started = None
        self._activity_started = None
        self._activity_pause_baseline = 0.0
        self.turn_clock.reset()
        self._activity = ""
        self._clear_retry()
        self._current_agent_type = None
        self._current_agent_uuid = None
        self._round_entries = []
        self._round_agent_type = None
        self._round_agent_uuid = None

    def _turn_elapsed(self, now: float) -> float:
        if self._turn_started is None:
            return 0.0
        return max(0.0, now - self._turn_started - self.turn_clock.paused_seconds(now))

    def _activity_elapsed(self, now: float) -> float:
        if self._activity_started is None:
            return 0.0
        paused = self.turn_clock.paused_seconds(now) - self._activity_pause_baseline
        return max(0.0, now - self._activity_started - paused)

    def _set_current_agent(self, agent_type: str | None, uuid: str | None) -> None:
        self._current_agent_type = agent_type
        self._current_agent_uuid = uuid

    def _active_agent_name(self) -> str:
        return self._agent_label(self._current_agent_type, self._current_agent_uuid) or "助手"

    @staticmethod
    def _agent_label(agent_type: str | None, uuid: str | None) -> str:
        if not agent_type:
            return ""
        short_uuid = uuid.split("-")[0] if uuid else ""
        return f"{agent_type} {short_uuid}" if short_uuid else agent_type

    def _response_prefix(self, event: ResponseDelta) -> str:
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        return f"回复({agent})：" if agent else "助手："

    def _thinking_prefix(self, event: ThinkingDelta) -> str:
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        return f"思考({agent})：" if agent else "思考"

    def reload_session_state(self) -> None:
        self._session_elapsed = 0.0
        self._reset_turn_status()
        self._agent_signature = ()
        self._main_focus_target = "composer"
        self._transcript_positions.clear()
        self.coordinator.reset()
        self.clear_selection()
        self.hide_completion()
        self.refresh_chrome()

    def on_plan_state_changed(self) -> None:
        self.refresh_chrome()

    def on_text_selected(self, _event: events.TextSelected) -> None:
        self._history.resume_anchor_at_end()
        if self.copy_on_select:
            self.call_after_refresh(self._copy_current_selection)

    def _selected_text(self) -> str | None:
        selection = self.screen.get_selected_text()
        if selection:
            return selection
        focused = self.focused
        if isinstance(focused, TextArea) and focused.selected_text:
            return focused.selected_text
        return None

    def _copy_current_selection(self) -> None:
        if selection := self._selected_text():
            self.copy_to_clipboard(selection)

    def copy_to_clipboard(self, text: str) -> None:
        if not self.native_clipboard_enabled or not self._native_clipboard.supported:
            super().copy_to_clipboard(text)
            return
        self._clipboard = text
        self._clipboard_pending = text
        if self._clipboard_worker_task is None or self._clipboard_worker_task.done():
            self._clipboard_worker_task = asyncio.create_task(
                self._drain_native_clipboard(),
                name="tui-native-clipboard",
            )

    async def _drain_native_clipboard(self) -> None:
        try:
            while self._clipboard_pending is not None:
                text = self._clipboard_pending
                self._clipboard_pending = None
                try:
                    copied = await self._native_clipboard.copy_async(text)
                except Exception:
                    copied = False
                if not copied and self._clipboard_pending is None:
                    super().copy_to_clipboard(text)
        finally:
            self._clipboard_worker_task = None

    def action_ctrl_c(self) -> None:
        if self.target_platform == "win32" and (selection := self._selected_text()):
            self.copy_to_clipboard(selection)
            return
        normal_input = (
            self.coordinator.input_active
            and not self.viewing_agent_id
            and not self.coordinator.modal_active
            and not self._agent_list.has_focus
        )
        if normal_input:
            if self._composer.text:
                self._composer.clear()
                self.hide_completion()
            else:
                self.coordinator.cancel_input_for_exit()
            return
        self.request_interrupt()

    def action_ctrl_d(self) -> None:
        if (
            self.coordinator.input_active
            and not self.viewing_agent_id
            and not self.coordinator.modal_active
            and not self._composer.text
        ):
            self.coordinator.cancel_input_for_exit()

    def action_copy_selection(self) -> None:
        self._copy_current_selection()

    def action_toggle_plan(self) -> None:
        if (
            not self.coordinator.input_active
            or self.viewing_agent_id
            or self.coordinator.modal_active
            or self.completion_visible
            or self._agent_list.has_focus
        ):
            return
        self.toggle_plan()
        self.refresh_chrome()

    def action_clear_selection(self) -> None:
        self.clear_selection()
