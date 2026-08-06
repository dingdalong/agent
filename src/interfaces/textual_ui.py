"""Textual UserInterface 实现。"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from src.events.menu import FormQuestion, UiRequest
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
from src.interfaces.base import UserInterface
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.app import AgentTuiApp
from src.interfaces.tui.diagnostics import TuiDiagnostics
from src.tools.display import permission_line
from src.interfaces.tui.dialogs import PendingInteractions
from src.interfaces.tui.history_journal import PlainHistoryJournal
from src.interfaces.tui.plain import PlainFrontend, normalize_line_input

if TYPE_CHECKING:
    from src.mgr.data_guard import DataGuard


@dataclass(slots=True)
class TuiTermination:
    kind: str
    error: BaseException | None
    task_error: BaseException | None
    fatal_error: BaseException | None
    internal_exception: BaseException | None
    return_code: int
    exit_requested: bool
    app_running: bool


def _restore_vscode_keyboard_protocol(is_tty: bool) -> None:
    """清除 VS Code Terminal 可能残留的 Kitty 键盘协议 flags。"""
    if not is_tty or os.environ.get("TERM_PROGRAM", "").lower() != "vscode":
        return
    stream = getattr(sys, "__stderr__", None)
    if stream is None:
        return
    try:
        stream.write("\x1b[=0u")
        stream.flush()
    except (OSError, ValueError):
        pass


class TextualInterface(UserInterface):
    """在 TTY 中运行 Textual，在管道环境中使用纯文本输入输出。"""

    def __init__(
        self,
        agent_view_store: AgentViewStore,
        slash_commands: list[tuple[str, str]] | None = None,
        turn_clock: TurnClock | None = None,
        *,
        copy_on_select: bool | None = None,
        diagnostic_dir: Path | None = None,
        data_guard: DataGuard | None = None,
    ) -> None:
        super().__init__()
        self.agent_view_store = agent_view_store
        self.slash_commands = slash_commands or []
        self.turn_clock = turn_clock or TurnClock()
        self.copy_on_select = copy_on_select
        self.data_guard = data_guard
        self.diagnostics = TuiDiagnostics(diagnostic_dir, data_guard)
        self._terminal_is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        self.is_tty = self._terminal_is_tty
        self._app: AgentTuiApp | None = None
        self._app_task: asyncio.Task | None = None
        self._pending_cancel: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None
        self._ui_ready = asyncio.Event()
        self._termination_handled = False
        self._stopping = False
        self.history_journal = PlainHistoryJournal()
        self._pending_interactions: PendingInteractions | None = None
        self._plain = PlainFrontend()
        self._plan_toggle_handler: Callable[[], None] | None = None
        self._input_history_provider: Callable[[], list[str]] | None = None
        self._plain_response_active = False
        self._plain_thinking_active = False

    @property
    def app(self) -> AgentTuiApp | None:
        """返回已启动的 Textual App，供 headless 集成测试使用。"""
        return self._app

    def set_slash_commands(self, items: list[tuple[str, str]]) -> None:
        """原位更新补全数据源；运行中的 AgentTuiApp 持有同一 list 引用，即时生效。"""
        self.slash_commands[:] = items

    async def start(self) -> None:
        if not self.is_tty or self._app_task is not None:
            return
        self._stopping = False
        self._termination_handled = False
        self.diagnostics.record("interface_started")
        await self._launch_app()

    async def _launch_app(self) -> None:
        self._ui_ready.clear()
        self.diagnostics.record("app_launching")
        # Textual 启动时会把当前终端属性快照为恢复基线，退出时原样写回。若继承到
        # 上一次异常退出残留的 raw 模式，脏状态就会被当作基线在多次运行间自我延续。
        # 这里先规范化再交给 Textual，确保它快照到的是干净基线（有意不还原）。
        if self._terminal_is_tty:
            normalize_line_input()
        self._app = AgentTuiApp(
            self.agent_view_store,
            self.slash_commands,
            self.turn_clock,
            self._request_user_interrupt,
            self.get_plan_state,
            self._toggle_plan,
            self._get_input_history,
            get_model_info=self.get_model_info,
            copy_on_select=self.copy_on_select,
            history_journal=self.history_journal,
            diagnostics=self.diagnostics,
        )
        app = self._app
        task = self._app_task = asyncio.create_task(app.run_async())
        task.add_done_callback(
            lambda finished, current_app=app:
            self._on_app_task_done(finished, current_app)
        )
        ready_task = asyncio.create_task(app.ready.wait())
        done, _pending = await asyncio.wait(
            {task, ready_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        ready_task.cancel()
        await asyncio.gather(ready_task, return_exceptions=True)
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            fallback = self._fallback_task
            if fallback is not None:
                await fallback
            return
        self._ui_ready.set()
        self.diagnostics.record("app_ready")

    def _on_app_task_done(
        self,
        task: asyncio.Task,
        app: AgentTuiApp | None = None,
    ) -> None:
        if self._termination_handled:
            return
        self._termination_handled = True
        current_app = app or self._app
        task_error: BaseException | None = None
        try:
            if not task.cancelled():
                task_error = task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass
        fatal_error = getattr(current_app, "fatal_error", None)
        internal_exception = getattr(current_app, "_exception", None)
        if not isinstance(fatal_error, BaseException):
            fatal_error = None
        if not isinstance(internal_exception, BaseException):
            internal_exception = None
        return_code = int(getattr(current_app, "return_code", 0) or 0)
        error = task_error or fatal_error or internal_exception
        if task.cancelled():
            kind = "task_cancelled"
        elif task_error is not None:
            kind = "task_exception"
        elif fatal_error is not None:
            kind = "textual_fatal"
        elif internal_exception is not None:
            kind = "textual_internal_exception"
        elif return_code:
            kind = "nonzero_return_code"
        else:
            kind = "unexpected_return"
        termination = TuiTermination(
            kind=kind,
            error=error,
            task_error=task_error,
            fatal_error=fatal_error,
            internal_exception=internal_exception,
            return_code=return_code,
            exit_requested=bool(getattr(current_app, "_exit", False)),
            app_running=bool(getattr(current_app, "is_running", False)),
        )
        self._record_termination(termination, current_app)
        if self._stopping:
            return
        self._ui_ready.clear()
        self._fallback_task = asyncio.create_task(
            self._fallback_to_plain(current_app, termination),
            name="tui-plain-fallback",
        )

    def _record_termination(
        self,
        termination: TuiTermination,
        app: AgentTuiApp | None,
    ) -> None:
        fields = {
            "termination_kind": termination.kind,
            "task_exception_type": (
                type(termination.task_error).__name__
                if termination.task_error is not None
                else None
            ),
            "fatal_exception_type": (
                type(termination.fatal_error).__name__
                if termination.fatal_error is not None
                else None
            ),
            "internal_exception_type": (
                type(termination.internal_exception).__name__
                if termination.internal_exception is not None
                else None
            ),
            "return_code": termination.return_code,
            "exit_requested": termination.exit_requested,
            "app_running": termination.app_running,
            "stopping": self._stopping,
            "ui_ready": self._ui_ready.is_set(),
            "viewing_transcript": bool(getattr(app, "viewing_agent_id", None)),
            "modal_active": bool(
                getattr(getattr(app, "coordinator", None), "modal_active", False)
            ),
            "response_stream_active": getattr(app, "_response_stream", None) is not None,
            "thinking_stream_active": getattr(app, "_thinking_stream", None) is not None,
        }
        if termination.error is not None:
            self.diagnostics.record_exception(
                "app_terminated",
                termination.error,
                **fields,
            )
        else:
            self.diagnostics.record(
                "app_terminated",
                **fields,
            )

    async def _fallback_to_plain(
        self,
        app: AgentTuiApp | None,
        termination: TuiTermination,
    ) -> None:
        if self._stopping or not self.is_tty:
            return
        self.is_tty = False
        try:
            if app is not None:
                self._pending_interactions = app.coordinator.detach_for_fallback()
            task = self._app_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            _restore_vscode_keyboard_protocol(self._terminal_is_tty)
            self.diagnostics.record(
                "plain_fallback_activated",
                termination_kind=termination.kind,
            )
            error = termination.error
            summary = termination.kind
            if error is not None:
                summary = f"{type(error).__name__}: {self._redact_text(str(error))}"
            self._plain.write(
                f"\nTUI 异常，已切换到文字模式（{self._diagnostic_hint()}）："
                f"{summary}\n"
            )
            history = self.history_journal.snapshot()
            if history:
                self._plain.write(history)
            await self._drain_pending_interactions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.diagnostics.record_exception(
                "plain_fallback_failed",
                exc,
            )
            self._cancel_pending_interactions()
        finally:
            self._ui_ready.set()

    def _redact_text(self, value: str) -> str:
        if self.data_guard is None:
            return value[:500]
        try:
            return str(self.data_guard.redact(value))[:500]
        except Exception:
            return "<unavailable>"

    def _diagnostic_hint(self) -> str:
        path = self.diagnostics.path
        location = str(path) if path is not None else "未启用日志"
        return f"诊断 ID {self.diagnostics.diagnostic_id}；日志 {location}"

    async def _drain_pending_interactions(self) -> None:
        snapshot = self._pending_interactions
        if snapshot is None:
            return
        view_request = snapshot.view_request
        if (
            view_request is not None
            and view_request.future is not None
            and not view_request.future.done()
        ):
            view_request.complete("")
        requests = [snapshot.active, *snapshot.queue]
        for request in requests:
            if request is None or request.future is None or request.future.done():
                continue
            await self._settle_ui_request(request)
        if self._pending_interactions is snapshot:
            self._pending_interactions = None

    def _cancel_pending_interactions(self) -> None:
        snapshot = self._pending_interactions
        self._pending_interactions = None
        if snapshot is None:
            return
        for request in [snapshot.active, *snapshot.queue, snapshot.view_request]:
            if request is not None:
                request.cancel()

    def _ui_alive(self) -> bool:
        return (
            self.is_tty
            and self._app is not None
            and not self._stopping
            and self._ui_ready.is_set()
            and (self._app_task is None or not self._app_task.done())
        )

    async def stop(self) -> None:
        self._stopping = True
        self._ui_ready.set()
        fallback = self._fallback_task
        if fallback is not None and not fallback.done():
            fallback.cancel()
        app = self._app
        task = self._app_task
        pending_cancel = self._pending_cancel
        try:
            if app is not None and task is not None and not task.done():
                await app.shutdown_ui()
            elif app is not None and hasattr(app, "coordinator"):
                app.coordinator.cancel_all(render=False)
            if pending_cancel is not None and not pending_cancel.done():
                pending_cancel.cancel()
            if pending_cancel is not None:
                await asyncio.gather(pending_cancel, return_exceptions=True)
            if fallback is not None:
                await asyncio.gather(fallback, return_exceptions=True)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        finally:
            self._cancel_pending_interactions()
            if self._active_user_request is not None:
                self._active_user_request.cancel()
                self._active_user_request = None
            self._app = None
            self._app_task = None
            self._fallback_task = None
            self._ui_ready.clear()
            _restore_vscode_keyboard_protocol(self._terminal_is_tty or self.is_tty)
            # Textual 的属性恢复失败时的兜底：不把 raw 模式留给用户 shell 和下次启动。
            if self._terminal_is_tty or self.is_tty:
                normalize_line_input()
            try:
                await super().stop()
            finally:
                await asyncio.shield(asyncio.to_thread(self.diagnostics.close))

    def reload(self) -> None:
        self._plain_response_active = False
        self._plain_thinking_active = False
        if self._ui_alive() and self._app is not None:
            self._app.call_later(self._app.reload_session_state)

    def set_plan_toggle_handler(self, handler: Callable[[], None] | None) -> None:
        self._plan_toggle_handler = handler

    def _toggle_plan(self) -> None:
        if self._plan_toggle_handler is not None:
            self._plan_toggle_handler()

    def on_plan_state_changed(self) -> None:
        if self._ui_alive() and self._app is not None:
            self._app.call_later(self._app.on_plan_state_changed)

    def set_input_history_provider(
        self, provider: Callable[[], list[str]] | None
    ) -> None:
        self._input_history_provider = provider

    def _get_input_history(self) -> list[str]:
        if self._input_history_provider is None:
            return []
        return self._input_history_provider()

    def refresh_input_history(self) -> None:
        if self._ui_alive() and self._app is not None:
            self._app.call_later(self._app.refresh_input_history)

    def cancel_active_input(self) -> bool:
        if self.is_tty and self._app is not None:
            if not self._ui_alive():
                return self._app.coordinator.cancel_all(render=False)
            changed = bool(
                self._app.coordinator.active
                or self._app.coordinator.queue
                or self._app.coordinator.view_request
                or self._app.viewing_agent_id
            )
            pending = self._pending_cancel
            if pending is None or pending.done():
                pending = asyncio.create_task(
                    self._app.invoke(self._app.coordinator.cancel_all)
                )
                self._pending_cancel = pending
                pending.add_done_callback(self._clear_pending_cancel)
            return changed
        return super().cancel_active_input()

    async def wait_interactions_idle(self) -> None:
        pending = self._pending_cancel
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        fallback = self._fallback_task
        if fallback is not None and fallback is not asyncio.current_task():
            await asyncio.gather(fallback, return_exceptions=True)
        if self.is_tty and self._app is not None:
            await self._app.coordinator.wait_idle()

    def _clear_pending_cancel(self, task: asyncio.Task) -> None:
        if self._pending_cancel is task:
            self._pending_cancel = None
        if not task.cancelled():
            task.exception()

    async def _wait_for_frontend_transition(self) -> None:
        if self._stopping:
            return
        if (
            self.is_tty
            and self._app_task is not None
            and self._app_task.done()
            and not self._termination_handled
        ):
            await asyncio.sleep(0)
        fallback = self._fallback_task
        if fallback is not None and not fallback.done():
            if fallback is not asyncio.current_task():
                await self._ui_ready.wait()
            return
        if (
            self.is_tty
            and not self._ui_alive()
            and (self._app_task is None or not self._app_task.done())
        ):
            await self._ui_ready.wait()

    async def _accept_ui_request(self, request: UiRequest) -> bool:
        await self._wait_for_frontend_transition()
        if not self._ui_alive() or self._app is None:
            return bool(request.future is not None and request.future.done())
        accepted = await self._invoke(
            lambda: self._app.coordinator.submit(request)
        )
        return accepted or bool(request.future is not None and request.future.done())

    async def _invoke(self, callback: Callable[[], object]) -> bool:
        await self._wait_for_frontend_transition()
        app = self._app
        app_task = self._app_task
        if not self._ui_alive() or app is None or app_task is None:
            return False
        invocation = asyncio.create_task(app.invoke(callback))
        try:
            done, _pending = await asyncio.wait(
                {invocation, app_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if invocation in done:
                await invocation
                return True
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await asyncio.sleep(0)
            fallback = self._fallback_task
            if fallback is not None:
                await fallback
        except (RuntimeError, LookupError):
            return False
        return False

    async def _write(self, message: str | Text, markdown: bool = False) -> None:
        if not self.is_tty:
            self._plain.write(message)
            return
        if not await self._invoke(lambda: self._app.append_output(message, markdown)):
            self._plain.write(message)

    async def _read_input(
        self,
        prompt: str,
        default: str = "",
        markdown: bool = False,
    ) -> str:
        del markdown
        return await self._plain.read_input(prompt, default)

    async def _read_permission(self, tool_name: str, detail: str, reason: str = "") -> str:
        return await self._plain.read_permission(tool_name, detail, reason)

    async def _read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        markdown: bool = False,
    ) -> str:
        del markdown
        return await self._plain.read_choice(prompt, options, default_index)

    async def _read_model_selection(
        self,
        prompt: str,
        models: list[tuple[str, str]],
        efforts: list[str],
        model_index: int,
        effort_index: int,
        markdown: bool = False,
    ) -> str:
        del markdown
        return await self._plain.read_model_selection(
            prompt, models, efforts, model_index, effort_index
        )

    async def _read_form(
        self,
        prompt: str,
        questions: list[FormQuestion],
        markdown: bool = False,
    ) -> str:
        del markdown
        return await self._plain.read_form(prompt, questions)

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        del markdown
        return await self._plain.read_choice_input(
            prompt,
            options,
            descriptions,
            input_placeholder,
            default_index,
        )

    async def _read_transcript_view(self, uuid: str) -> str:
        del uuid
        return ""

    async def _end_response_if_needed(self) -> None:
        if self.is_tty:
            app = self._app
            if app is not None and await self._invoke(app.end_response):
                return
        if self._plain_response_active:
            self._plain.write("\n")
            self._plain_response_active = False

    async def _end_thinking_if_needed(self) -> None:
        if self.is_tty:
            app = self._app
            if app is not None and await self._invoke(app.end_thinking):
                return
        if self._plain_thinking_active:
            self._plain.write("\n")
            self._plain_thinking_active = False

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_response_delta(event, content)):
                return
        if not self._plain_response_active:
            self._plain.write(f"\n› {self._stream_label(event, '助手')}\n")
            self._plain_response_active = True
        self._plain.write(content)

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_thinking_delta(event, content)):
                return
        if not self._plain_thinking_active:
            self._plain.write(f"\n› {self._stream_label(event, '思考')}\n")
            self._plain_thinking_active = True
        self._plain.write(content)

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        if self.is_tty:
            await self._invoke(lambda: self._app.on_llm_call_started(event))

    async def on_llm_retrying(self, event: LLMRetrying) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_llm_retrying(event)):
                return
        remaining = max(0, math.ceil(event.wait_seconds))
        self._plain.write(
            f"⚠ LLM 调用失败 [{event.error_kind}] {event.safe_message}；"
            f"{remaining}秒后重试 ({event.attempt}/{event.max_attempts})\n"
        )

    async def on_llm_length_retrying(self, event: LLMLengthRetrying) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_llm_length_retrying(event)):
                return
        action = {
            "regenerate-lower-effort": f"降低推理力度至 {event.effort} 后重生成",
            "regenerate-compress": "压缩思考后重生成",
        }.get(event.strategy, "从中断处继续生成")
        self._plain.write(
            f"⚠ 输出截断（{event.truncation_kind}）：{action} "
            f"({event.attempt}/{event.max_attempts})\n"
        )

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_llm_call_failed(event)):
                return
        identifiers = []
        if event.request_id:
            identifiers.append(f"request_id={event.request_id}")
        if event.diagnostic_id:
            identifiers.append(f"diagnostic_id={event.diagnostic_id}")
        suffix = f" ({', '.join(identifiers)})" if identifiers else ""
        self._plain.write(
            f"✘ LLM 调用失败 [{event.error_kind}] {event.safe_message}{suffix}\n"
        )

    async def on_compact_delta(self, event: CompactDelta) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_compact_delta(event)):
                return
        self._plain.write(f"[compact] {event.content.strip() or 'context'}\n")

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_permission_notice(event)):
                return
        self._plain.write(
            permission_line(
                event.status, event.tool_name, event.detail, event.decision_source
            )
            + "\n"
        )

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_tool_call_started(event)):
                return
        # 非 TTY：优先使用 display
        display = event.display
        if display is not None and hasattr(display, "title"):
            title = display.title
            content = (display.content or "").strip()
            line = f"● {title}"
            if content:
                line += f"  {content.splitlines()[0]}"
            self._plain.write(f"{line}\n")
        else:
            detail = f" {event.detail.strip()}" if event.detail.strip() else ""
            self._plain.write(f"● {event.tool_name}{detail}\n")

    async def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        if self.is_tty:
            if await self._invoke(lambda: self._app.on_tool_call_completed(event)):
                return
        ok = event.status == "success"
        # 非 TTY：优先使用 display
        display = event.display
        if display is not None and hasattr(display, "title") and hasattr(display, "content"):
            mark = "✔" if ok else "✘"
            title = display.title or event.tool_name
            self._plain.write(f"  {mark} {title}  ({event.duration_seconds:.2f}s)\n")
            content = (display.content or "").strip()
            if content:
                for line in content.splitlines()[:20]:
                    self._plain.write(f"  {line}\n")
        else:
            preview = (event.result_preview or "").strip().splitlines()
            first = preview[0] if preview else ("完成" if ok else "失败")
            self._plain.write(f"  ⎿ {first}  ({event.duration_seconds:.2f}s)\n")
            if not ok and len(preview) > 1:
                self._plain.write("\n".join(preview[1:]) + "\n")

    async def _emit_caller_banner(
        self,
        caller_agent_type: str | None,
        caller_uuid: str | None,
    ) -> None:
        if self.is_tty or not caller_agent_type:
            return
        short_uuid = caller_uuid.split("-")[0] if caller_uuid else ""
        label = f"{caller_agent_type} {short_uuid}" if short_uuid else caller_agent_type
        self._plain.write(f"\n› {label}\n")

    @staticmethod
    def _stream_label(event: ResponseDelta | ThinkingDelta, fallback: str) -> str:
        if not event.caller_agent_type:
            return fallback
        short_uuid = event.caller_uuid.split("-")[0] if event.caller_uuid else ""
        label = (
            f"{event.caller_agent_type} {short_uuid}"
            if short_uuid
            else event.caller_agent_type
        )
        return f"{fallback}({label})"
