"""内嵌交互 FIFO、转录请求与 Future 生命周期的协调器。

控件只产出 `DialogResult`；排队、挂载、结算、降级转交集中在这里，
使聊天历史的渲染与交互状态机彼此独立。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.events.menu import (
    InputMenu,
    MenuRequest,
    TranscriptView,
    UiRequest,
    ViewRequest,
)
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.interactions import (
    DialogResult,
    InlineWidget,
    make_inline_widget,
)

if TYPE_CHECKING:
    from src.interfaces.tui.app import AgentTuiApp


@dataclass(slots=True)
class PendingInteractions:
    """TUI 降级到文字模式时转交的未完成请求。"""

    active: MenuRequest | None
    queue: list[MenuRequest]
    view_request: ViewRequest | None


class InteractionCoordinator:
    """管理普通输入、内嵌交互 FIFO、Transcript 和请求 Future 生命周期。"""

    def __init__(self, app: AgentTuiApp, turn_clock: TurnClock) -> None:
        self.app = app
        self.turn_clock = turn_clock
        self.active: MenuRequest | None = None
        self.queue: deque[MenuRequest] = deque()
        self.view_request: ViewRequest | None = None
        self.inline_widget: InlineWidget | None = None
        self._cleanup_tasks: set[asyncio.Future[None]] = set()
        self._closed = False
        self._detached = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def inline_widget_active(self) -> bool:
        return self.inline_widget is not None

    @property
    def input_active(self) -> bool:
        return isinstance(self.active, InputMenu)

    @property
    def pending_summary(self) -> tuple[int, str | None]:
        self._drop_finished_queue_items()
        if not self.queue:
            return 0, None
        request = self.queue[0]
        return len(self.queue), request.caller_agent_type or request.source

    @property
    def is_idle(self) -> bool:
        return self.active is None and not self.queue and not self._cleanup_tasks

    async def submit(self, request: UiRequest) -> bool:
        if request.future is None or request.future.done():
            return False
        if self._closed or self._detached:
            request.cancel()
            return False
        request.future.add_done_callback(
            lambda _future, item=request: self._schedule(self._request_settled(item))
        )
        if isinstance(request, ViewRequest):
            await self._open_view(request)
            return True
        if not isinstance(request, MenuRequest):
            raise TypeError(f"unsupported UI request: {type(request)!r}")
        self.queue.append(request)
        self._idle.clear()
        await self._pump()
        self.app.refresh_chrome()
        return True

    async def _pump(self) -> None:
        if self._closed or self.active is not None:
            self._sync_idle()
            return
        self._drop_finished_queue_items()
        if not self.queue:
            self._sync_idle()
            return
        request = self.queue.popleft()
        self.active = request
        await self.app.record_request_context(request)
        if isinstance(request, InputMenu):
            await self.app.begin_input(request)
            self.app.refresh_chrome()
            return
        self.turn_clock.enter_human_wait()
        try:
            snapshot = self.app.agent_view_store.agent_snapshot(request.caller_uuid or "")
            task = snapshot.task if snapshot else ""
            self.inline_widget = make_inline_widget(request, task=task)
            await self.app.mount_inline_widget(self.inline_widget)
        except BaseException as exc:
            self.turn_clock.exit_human_wait()
            self.active = None
            self.inline_widget = None
            request.fail(exc)
            await self._pump()
            raise
        self.app.refresh_chrome()

    async def complete_input(self, text: str) -> bool:
        request = self.active
        if not isinstance(request, InputMenu) or not text.strip():
            return False
        await self.app.finish_input(text)
        self.active = None
        request.complete(text)
        await self._pump()
        self.app.refresh_chrome()
        return True

    def cancel_input_for_exit(self) -> bool:
        request = self.active
        if not isinstance(request, InputMenu):
            return False
        self.app.finish_input_cancelled()
        self.active = None
        request.cancel()
        self._schedule(self._pump())
        self.app.refresh_chrome()
        return True

    async def _finish_inline_widget(self, result: DialogResult) -> None:
        """内嵌交互完成后：按 Widget 策略写摘要并结算 Future。"""
        request = self.active
        if request is None or isinstance(request, InputMenu):
            return
        widget = self.inline_widget
        self.inline_widget = None
        self.turn_clock.exit_human_wait()
        self.active = None
        if widget is not None:
            summary = widget.build_summary(result)
            await self.app.unmount_inline_widget(widget)
            if summary:
                await self.app.append_output(summary)
        await asyncio.sleep(0)
        if request.future is not None and not request.future.done():
            if result.cancelled:
                request.complete("")
            else:
                request.complete(result.value)
        await self._pump()
        self.app.restore_focus()
        self.app.refresh_chrome()

    async def _open_view(self, request: ViewRequest) -> None:
        if not isinstance(request, TranscriptView):
            raise TypeError(f"unsupported view request: {type(request)!r}")
        previous = self.view_request
        if previous is not None and previous is not request:
            previous.cancel()
        self.view_request = request
        opened = await self.app.transcript.open(
            request.uuid,
            [snapshot.uuid for snapshot in self.app.agent_view_store.subagent_snapshots()],
            invoked=True,
        )
        if not opened and self.view_request is request:
            self.view_request = None
            request.complete("")

    async def open_live_transcript(self, uuid: str) -> bool:
        if self._closed:
            return False
        ids = [
            snapshot.uuid
            for snapshot in self.app.agent_view_store.subagent_snapshots()
        ]
        return await self.app.transcript.open(uuid, ids, invoked=False)

    def close_transcript(self) -> bool:
        if self.app.transcript.viewing_agent_id is None:
            return False
        self.app.transcript.hide()
        request = self.view_request
        self.view_request = None
        if request is not None:
            request.complete("")
        self.app.restore_focus()
        return True

    def cancel_all(self, *, render: bool = True) -> bool:
        changed = bool(
            self.active or self.queue or self.view_request or self.app.transcript.viewing_agent_id
        )
        queued = list(self.queue)
        self.queue.clear()
        for request in queued:
            request.cancel()
        if isinstance(self.active, InputMenu):
            request = self.active
            self.active = None
            if render:
                self.app.finish_input_cancelled()
            request.cancel()
        elif self.active is not None:
            request = self.active
            widget = self.inline_widget
            self.active = None
            self.inline_widget = None
            if render:
                self.turn_clock.exit_human_wait()
            request.cancel()
            if render and widget is not None:
                self._schedule(self.app.unmount_inline_widget(widget))
        if self.view_request is not None:
            self.view_request.cancel()
            self.view_request = None
        if render and self.app.transcript.viewing_agent_id is not None:
            self.app.transcript.hide()
        if not render:
            self.active = None
            self.inline_widget = None
            self.turn_clock.reset()
        self._sync_idle()
        if render:
            self.app.refresh_chrome()
        return changed

    async def wait_idle(self) -> None:
        while not self.is_idle:
            await self._idle.wait()
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        self.cancel_all()
        await self.wait_idle()

    def detach_for_fallback(self) -> PendingInteractions:
        """脱离已退出的 Textual app，并把未完成请求交给文字前端。"""
        snapshot = PendingInteractions(
            active=self.active,
            queue=list(self.queue),
            view_request=self.view_request,
        )
        self._detached = True
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        self.active = None
        self.queue.clear()
        self.view_request = None
        self.inline_widget = None
        self.turn_clock.reset()
        self._sync_idle()
        return snapshot

    def _render_available(self) -> bool:
        return bool(getattr(self.app, "is_running", False))

    async def _request_settled(self, request: UiRequest) -> None:
        if self._detached:
            return
        if request is self.view_request:
            self.view_request = None
            if self._render_available() and self.app.transcript.viewing_agent_id is not None:
                self.app.transcript.hide()
            return
        if request is self.active:
            if isinstance(request, InputMenu):
                if self._render_available():
                    self.app.finish_input_cancelled()
                self.active = None
                await self._pump()
            elif self.inline_widget is not None:
                if self._render_available() and self.inline_widget.is_mounted:
                    self.inline_widget.action_cancel()
            return
        try:
            self.queue.remove(request)  # type: ignore[arg-type]
        except ValueError:
            return
        await self._pump()

    def reset(self) -> None:
        if self.is_idle and self.app.transcript.viewing_agent_id is not None:
            self.close_transcript()

    def _drop_finished_queue_items(self) -> None:
        self.queue = deque(
            request
            for request in self.queue
            if request.future is not None and not request.future.done()
        )

    def _schedule(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._cleanup_tasks.add(task)
        self._idle.clear()
        task.add_done_callback(self._cleanup_finished)

    def _cleanup_finished(self, task: asyncio.Future[None]) -> None:
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._sync_idle()

    def _sync_idle(self) -> None:
        if self.is_idle:
            self._idle.set()
        else:
            self._idle.clear()
