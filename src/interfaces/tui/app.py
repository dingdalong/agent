"""生产 Textual TUI 应用。"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static, TextArea

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
from src.interfaces.status_presenter import agent_label, present_agent
from src.interfaces.turn_clock import TurnClock
from src.common.tool_display import permission_line
from src.interfaces.tui import session_replay
from src.interfaces.tui.chrome import ChromeRenderer, rich_text_signature
from src.interfaces.tui.diagnostics import TuiDiagnostics
from src.interfaces.tui.coordinator import InteractionCoordinator
from src.interfaces.tui.event_lines import (
    THINKING_CONTENT_STYLE,
    DisplayBlock,
    RoundEntry,
    blocks_for_event,
    round_entry_text,
    stream_label_block,
    user_block,
)
from src.interfaces.tui.history_log import HistoryLog, strip_trailing_newlines
from src.interfaces.tui.history_journal import PlainHistoryJournal
from src.interfaces.tui.input_area import InputAreaController
from src.interfaces.tui.interactions import InlineWidget
from src.interfaces.tui.render_policy import TuiRenderPolicy
from src.interfaces.tui.transcript import TranscriptController
from src.common.session_state import SessionRecord
from src.interfaces.tui.widgets import (

    AgentList,
    Composer,
    KeyboardListItem,
    NativeClipboard,
    SelectionScreen,
    SelectionStatic,
)


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


def _static_content_signature(content: str | Text) -> tuple[Any, ...]:
    if isinstance(content, Text):
        return "rich", *rich_text_signature(content)
    return "plain", content


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
        get_input_history: Callable[[], list[str]] | None = None,
        *,
        get_model_info: Callable[[], tuple[str, str] | None] | None = None,
        copy_on_select: bool | None = None,
        platform_name: str | None = None,
        native_clipboard: bool = True,
        history_journal: PlainHistoryJournal | None = None,
        diagnostics: TuiDiagnostics | None = None,
        render_policy: TuiRenderPolicy | None = None,
    ) -> None:
        # ansi_color=True：禁用 ANSIToTruecolor 行过滤器，让 ansi_default 背景输出
        # SGR 49（终端真实默认背景），而非被改写为 ansi_theme 的固定 RGB。
        super().__init__(ansi_color=True)
        self.agent_view_store = agent_view_store
        self.slash_commands = slash_commands
        self.turn_clock = turn_clock
        self.request_interrupt = request_interrupt
        self.get_plan_state = get_plan_state
        self.toggle_plan = toggle_plan
        self.get_input_history = get_input_history or (lambda: [])
        self.get_model_info = get_model_info or (lambda: None)
        self.target_platform = platform_name or sys.platform
        self.copy_on_select = (
            self.target_platform == "darwin"
            if copy_on_select is None
            else copy_on_select
        )
        self.native_clipboard_enabled = native_clipboard
        self._native_clipboard = NativeClipboard(self.target_platform)
        self.render_policy = render_policy or TuiRenderPolicy()
        self.history_journal = history_journal or PlainHistoryJournal(self.render_policy)
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
        self._response_stream: Any = None
        self._thinking_stream: Any = None

        self._agent_ids: list[str] = []
        self._agent_signature: tuple[Any, ...] | None = None
        self._agent_label_signatures: dict[str, tuple[Any, ...]] = {}
        self._main_focus_target = "composer"
        self.chrome = ChromeRenderer(self)
        self.input_area = InputAreaController(self)
        self.transcript = TranscriptController(self)
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

    def compose(self) -> ComposeResult:
        yield HistoryLog(
            id="history",
            policy=self.render_policy,
            diagnostics=self.diagnostics,
        )
        yield Vertical(id="interaction-slot")
        with Vertical(id="transient-zone"):
            yield SelectionStatic("", id="activity", markup=False)
            with self.transcript.zone:
                yield self.transcript.header
                with self.transcript.panel:
                    yield self.transcript.content
            yield self.input_area.completion
        yield SelectionStatic("", id="input-status", markup=False)
        yield SelectionStatic("", classes="separator", id="separator-top", markup=False)
        with self.input_area.shell:
            yield self.input_area.composer
        yield SelectionStatic("", classes="separator", id="separator-bottom", markup=False)
        yield SelectionStatic("", id="core-status", markup=False)
        yield AgentList(id="agent-list", initial_index=0)

    def get_default_screen(self) -> SelectionScreen:
        return SelectionScreen(id="_default")

    async def on_mount(self) -> None:
        self._history = self.query_one("#history", HistoryLog)
        self._interaction_slot = self.query_one("#interaction-slot", Vertical)
        self._activity_widget = self.query_one("#activity", Static)
        self._input_status = self.query_one("#input-status", Static)
        self._status = self.query_one("#core-status", Static)
        self._agent_list = self.query_one("#agent-list", AgentList)
        self._separator_top = self.query_one("#separator-top", Static)
        self._separator_bottom = self.query_one("#separator-bottom", Static)
        self.input_area.start()
        self.transcript.start()
        self.chrome.update_separators()
        self._tick_timer = self.set_interval(
            self.render_policy.activity_interval,
            self._tick,
        )
        self.refresh_chrome()
        self.ready.set()
        self.diagnostics.record(
            "app_mounted",
        )

    async def on_unmount(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        await self.transcript.stop()
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
        await self.transcript.stop()
        self.exit()

    def set_screen_class(self, add: bool, class_name: str) -> None:
        self.screen.set_class(add, class_name)

    def _tick(self) -> None:
        if not self._screen_stack:
            return
        self.chrome.update_separators()
        self._schedule_agent_refresh()
        self.transcript.schedule_refresh()
        if self.chrome.dirty:
            self.refresh_chrome()
        elif self.chrome.is_dynamic() and not self._history.reflow_pending:
            self.chrome.update_dynamic()

    async def append_output(
        self,
        message: str | Text,
        markdown: bool = False,
        *,
        style: str | None = None,
        spacing: int = 1,
    ) -> None:
        message = strip_trailing_newlines(message)
        self._history.append_entry(
            message,
            markdown=markdown and isinstance(message, str),
            style=style,
            spacing=spacing,
        )
        self.history_journal.append_entry(message)

    async def append_blocks(self, blocks: list[DisplayBlock]) -> None:
        """按块追加展示内容，样式与间距随块传入。"""
        for block in blocks:
            await self.append_output(
                block.content,
                block.markdown,
                style=block.style,
                spacing=block.spacing,
            )

    async def append_markdown(
        self,
        source: str,
        *,
        style: str | None = None,
        spacing: int = 1,
        stream_id: str | None = None,
    ) -> str:
        if stream_id is None:
            entry_id = self._history.append_entry(
                source,
                markdown=True,
                style=style,
                spacing=spacing,
            )
            self.history_journal.append_entry(source)
        else:
            entry_id = self._history.begin_stream(
                stream_id,
                source,
                markdown=True,
                style=style,
                spacing=spacing,
            )
            self.history_journal.start_stream(stream_id, source)
        return entry_id

    async def append_user(self, text: str) -> None:
        block = user_block(text)
        await self.append_output(block.content, style=block.style)

    async def record_request_context(self, request: MenuRequest) -> None:
        if isinstance(request, InputMenu):
            return
        label = agent_label(request.caller_agent_type, request.caller_uuid)
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
                    "\n" + permission_line("ask", request.tool_name, request.reason),
                    style="#efc36a",
                )
            await self.append_output(text)
            return
        prompt = getattr(request, "prompt", "")
        if prompt.strip():
            await self.append_output(prompt, markdown=getattr(request, "markdown", False))

    async def mount_inline_widget(self, widget: InlineWidget) -> None:
        """将唯一活动表单挂载到临时交互槽。"""
        await self.flush_round()
        await self._interaction_slot.remove_children()
        await self._interaction_slot.mount(widget)
        self._interaction_slot.display = True
        self.input_area.sync_state()
        self.call_after_refresh(self.restore_focus)

    async def unmount_inline_widget(self, widget: InlineWidget) -> None:
        if widget.is_mounted:
            await widget.remove()
        self._interaction_slot.display = False

    async def begin_input(self, request: InputMenu) -> None:
        await self.flush_round()
        self._session_elapsed += self._turn_elapsed(time.monotonic())
        self._main_focus_target = "composer"
        lines = request.prompt.splitlines()
        context = "\n".join(lines[:-1]).strip("\n") if len(lines) > 1 else ""
        if context.strip():
            await self.append_output(context, markdown=request.markdown)
        self.input_area.load_text(request.default, cursor_to_end=True)
        self.input_area.sync_state()
        self.call_after_refresh(self.restore_focus)

    async def finish_input(self, text: str) -> None:
        await self.append_user(text)
        self.input_area.clear()
        self._reset_turn_status()
        self.input_area.sync_state()

    def finish_input_cancelled(self) -> None:
        self.input_area.clear()
        self._reset_turn_status()
        self.input_area.sync_state()

    def focus_composer(self) -> None:
        self._main_focus_target = "composer"
        self.input_area.focus()

    def on_app_focus(self, _event: events.AppFocus) -> None:
        """终端窗口重新激活后，把键盘交还给当前交互面板。"""
        if hasattr(self, "_tick_timer"):
            self._tick_timer.resume()
            self._tick()
        self.call_after_refresh(self.restore_focus)

    def on_app_blur(self, _event: events.AppBlur) -> None:
        """终端失焦时暂停周期动画，静态事件仍可正常更新界面。"""
        if hasattr(self, "_tick_timer"):
            self._tick_timer.pause()

    def restore_focus(self) -> None:
        self.input_area.sync_state()
        if self.coordinator.inline_widget is not None:
            if self.coordinator.inline_widget.is_mounted:
                self.coordinator.inline_widget.restore_focus()
            return
        if self.transcript.viewing_agent_id:
            self.transcript.panel.focus()
        elif (
            self._main_focus_target == "agent_list"
            and self._has_sub_agents()
        ):
            self._agent_list.focus()
        else:
            self.focus_composer()

    def refresh_chrome(self) -> None:
        if not self.input_area.composer.is_mounted:
            return
        self.chrome.update()
        self.input_area.render_completion()
        self.input_area.sync_state()
        self._sync_agent_list_visibility()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self.input_area.composer:
            return
        self.input_area.on_text_changed(event.text_area.text)

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        if self.input_area.read_only or not event.text.strip():
            return
        await self.coordinator.complete_input(event.text)

    def on_inline_widget_completed(self, event: InlineWidget.Completed) -> None:
        """内嵌表单提交/取消后，触发 coordinator 完成流程。"""
        self.coordinator._schedule(
            self.coordinator._finish_inline_widget(event.result)
        )

    def _schedule_agent_refresh(self, now: float | None = None) -> None:
        snapshots = self._browser_snapshots()
        ids = [snapshot.uuid for snapshot in snapshots]
        structure_stable = (
            ids == self._agent_ids
            and len(self._agent_list.children) == len(ids)
        )
        if structure_stable and self._agent_list.display:
            render_time = time.monotonic() if now is None else now
        else:
            render_time = 0.0
        labels: list[str | Text] = [
            snapshot.agent_type
            if snapshot.is_main
            else present_agent(snapshot, now=render_time)
            for snapshot in snapshots
        ]
        label_signatures = [
            _static_content_signature(label)
            for label in labels
        ]
        signature = tuple(zip(ids, label_signatures))
        if signature == self._agent_signature:
            return
        self._agent_signature = signature
        if structure_stable:
            self._update_agent_list_labels(ids, labels, label_signatures)
            self._sync_agent_list_visibility()
            return
        self._run_presentation_worker(
            lambda: self._sync_agent_list(snapshots, labels, label_signatures),
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

    async def _sync_agent_list(
        self,
        snapshots,
        labels: list[str | Text],
        label_signatures: list[tuple[Any, ...]],
    ) -> None:
        ids = [snapshot.uuid for snapshot in snapshots]
        if (
            ids != self._agent_ids
            or len(self._agent_list.children) != len(ids)
        ):
            selected_uuid = (
                self._agent_ids[self._agent_list.index]
                if self._agent_list.index is not None
                and self._agent_list.index < len(self._agent_ids)
                else None
            )
            await self._agent_list.remove_children()
            for label in labels:
                await self._agent_list.mount(
                    KeyboardListItem(SelectionStatic(label, markup=False))
                )
            self._agent_ids = ids
            self._agent_label_signatures = dict(zip(ids, label_signatures))
            if ids:
                self._agent_list.index = ids.index(selected_uuid) if selected_uuid in ids else 0
        else:
            self._update_agent_list_labels(ids, labels, label_signatures)
        self._sync_agent_list_visibility()

    def _update_agent_list_labels(
        self,
        ids: list[str],
        labels: list[str | Text],
        label_signatures: list[tuple[Any, ...]],
    ) -> None:
        for item, uuid, content, signature in zip(
            self._agent_list.children,
            ids,
            labels,
            label_signatures,
        ):
            if self._agent_label_signatures.get(uuid) == signature:
                continue
            label = next(iter(item.query(Static)), None)
            if label is None:
                continue
            label.update(content, layout=False)
            self._agent_label_signatures[uuid] = signature

    def _sync_agent_list_visibility(self) -> None:
        if not hasattr(self, "_agent_list"):
            return
        has_sub_agents = self._has_sub_agents()
        self._agent_list.display = (
            has_sub_agents
            and not self.transcript.viewing_agent_id
            and not self.coordinator.inline_widget_active
        )
        if (
            not has_sub_agents
            and not self.transcript.viewing_agent_id
            and self._main_focus_target == "agent_list"
        ):
            self.focus_composer()

    def _has_sub_agents(self) -> bool:
        return self.agent_view_store.has_running_subagents()

    def _browser_snapshots(self):
        snapshots = self.agent_view_store.active_agent_snapshots()
        main = [s for s in snapshots if s.is_main]
        subagents = [s for s in snapshots if not s.is_main]
        return main + subagents

    def focus_agent_list(self) -> bool:
        if (
            self.transcript.viewing_agent_id
            or self.coordinator.inline_widget_active
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

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("回应中")
        if self._response_stream is None:
            await self.append_blocks([
                stream_label_block("response", event.caller_agent_type, event.caller_uuid),
            ])
            await self.append_markdown("", stream_id="response")
            self._response_stream = "response"
        self._history.append_stream("response", content)
        self.history_journal.append_stream("response", content)

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("思考中")
        if self._thinking_stream is None:
            await self.append_blocks([
                stream_label_block("thinking", event.caller_agent_type, event.caller_uuid),
            ])
            await self.append_markdown(
                "",
                style=THINKING_CONTENT_STYLE,
                stream_id="thinking",
            )
            self._thinking_stream = "thinking"
        self._history.append_stream("thinking", content)
        self.history_journal.append_stream("thinking", content)

    async def end_response(self) -> None:
        if self._response_stream is not None:
            self._history.end_stream("response")
            self._response_stream = None
        self.history_journal.end_stream("response")

    async def end_thinking(self) -> None:
        if self._thinking_stream is not None:
            self._history.end_stream("thinking")
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
        await self.append_blocks(blocks_for_event(event))
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
        await self.append_blocks(blocks_for_event(event))

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("失败")
        await self.append_blocks(blocks_for_event(event))

    async def on_compact_delta(self, event: CompactDelta) -> None:
        self._set_activity("压缩上下文")
        await self.append_blocks(blocks_for_event(event))

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        await self.append_blocks(blocks_for_event(event))

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity(event.tool_name)
        if event.caller_uuid != self.agent_view_store.foreground_uuid:
            return
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
                entry.status = event.status
                entry.preview = (event.result_preview or "").strip()
                entry.duration = event.duration_seconds
                entry.result_display = event.display
                break

    async def flush_round(self) -> None:
        if not self._round_entries:
            return
        for entry in self._round_entries:
            await self.append_output(round_entry_text(entry))
        self._round_entries = []

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
            self.chrome.mark_dirty()

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

    def set_focus_target(self, target: str) -> None:
        """记录转录收起后应恢复的逻辑焦点。"""
        self._main_focus_target = target

    @property
    def active_streams(self) -> tuple[bool, bool]:
        """(回复流, 思考流) 是否正在写入；转录诊断据此标注排队时刻的流状态。"""
        return self._response_stream is not None, self._thinking_stream is not None

    def _active_agent_name(self) -> str:
        return agent_label(self._current_agent_type, self._current_agent_uuid) or "助手"

    def reload_session_state(self) -> None:
        self._session_elapsed = 0.0
        self._reset_turn_status()
        self._agent_signature = None
        self._agent_label_signatures.clear()
        self._main_focus_target = "composer"
        self.transcript.reset()
        self.coordinator.reset()
        self.clear_selection()
        self.input_area.clear()
        self.input_area.reset_history()
        self.refresh_chrome()

    async def replace_session_history(
        self,
        records: list[SessionRecord],
        *,
        resolver: object | None = None,
    ) -> None:
        """把 SessionRecord.view 批量转换成单控件历史源。

        Args:
            records: 会话记录序列。
            resolver: 可选内容解析器，用于重建被引用化的展示正文。

        Returns:
            None。
        """
        await self.end_thinking()
        await self.end_response()
        await self.flush_round()
        self._round_entries.clear()
        entries = session_replay.history_entries(records, resolver=resolver)
        self._history.replace_entries(entries)
        await self._history.wait_for_reflow()
        self.history_journal.clear()
        for entry in entries:
            self.history_journal.append_entry(entry.content)
        self.reload_session_state()

    def on_plan_state_changed(self) -> None:
        self.chrome.mark_dirty()

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
        # Textual 的 App.clipboard 读取其私有 _clipboard；本路径不调用 super()，
        # 需自行维护该本地剪贴板副本。
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
            and not self.transcript.viewing_agent_id
            and not self.coordinator.inline_widget_active
            and not self._agent_list.has_focus
        )
        if normal_input:
            if self.input_area.text:
                self.input_area.clear()
            else:
                self.coordinator.cancel_input_for_exit()
            return
        self.request_interrupt()

    def action_ctrl_d(self) -> None:
        if (
            self.coordinator.input_active
            and not self.transcript.viewing_agent_id
            and not self.coordinator.inline_widget_active
            and not self.input_area.text
        ):
            self.coordinator.cancel_input_for_exit()

    def action_copy_selection(self) -> None:
        self._copy_current_selection()

    def action_toggle_plan(self) -> None:
        if (
            not self.coordinator.input_active
            or self.transcript.viewing_agent_id
            or self.coordinator.inline_widget_active
            or self.input_area.completion_visible
            or self._agent_list.has_focus
        ):
            return
        self.toggle_plan()
        self.chrome.mark_dirty()

    def action_clear_selection(self) -> None:
        self.clear_selection()
