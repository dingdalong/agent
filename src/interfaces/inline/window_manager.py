"""Internal window stack and FIFO request scheduler for the Inline TUI."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.events.menu import MenuRequest, UiRequest, ViewRequest


DialogRunner = Callable[[MenuRequest], Awaitable[str]]
WindowChangeHandler = Callable[[], None]


@dataclass(frozen=True, slots=True)
class DialogOutcome:
    """Terminal result returned by one started dialog runner.

    Attributes:
        answer: Successful dialog result.
        failure: Exception raised by the dialog reader.
        cancelled: Whether dialog reading ended through cancellation or user interruption.
    """

    answer: str = ""
    failure: BaseException | None = None
    cancelled: bool = False


@dataclass(slots=True)
class WindowEntry:
    """One currently visible or covered TUI window.

    Attributes:
        kind: Rendering and keyboard-routing kind of the window.
        request: Originating UI request; None only for a live transcript view.
        uuid: Viewed subagent UUID for transcript windows.
        scroll: Transcript scroll offset counted from the live tail.
    """

    kind: str
    request: UiRequest | None
    uuid: str | None = None
    scroll: int = 0


class WindowManager:
    """Own the active window stack, dialog queue, and runner lifecycle.

    A stack can contain one transcript window and one dialog window. Dialogs never
    preempt one another; waiting dialogs are started FIFO after the current runner
    has cleaned up its component state. A transcript may remain below a dialog.
    """

    def __init__(
        self,
        run_dialog: DialogRunner,
        on_change: WindowChangeHandler | None = None,
    ) -> None:
        """Initialize an empty manager.

        Args:
            run_dialog: Coroutine that renders and reads one active answer window.
            on_change: Synchronous callback used to invalidate the TUI after state changes.

        Returns:
            None.
        """
        self._run_dialog = run_dialog
        self._on_change = on_change or (lambda: None)
        self._stack: list[WindowEntry] = []
        self._queue: deque[MenuRequest] = deque()
        self._runner: asyncio.Task[DialogOutcome] | None = None
        self._runners: set[asyncio.Task[DialogOutcome]] = set()
        self._closed = False

    @property
    def window_stack(self) -> tuple[WindowEntry, ...]:
        """Return the current bottom-to-top window entries.

        Returns:
            Immutable snapshot of currently retained window entries.
        """
        return tuple(self._stack)

    @property
    def active_window(self) -> WindowEntry | None:
        """Return the retained answer window, even if a transcript covers it.

        Returns:
            Dialog entry or None when only the base/transcript is visible.
        """
        for entry in reversed(self._stack):
            if isinstance(entry.request, MenuRequest):
                return entry
        return None

    @property
    def top_window(self) -> WindowEntry | None:
        """Return the visually topmost retained window.

        Returns:
            Last stack entry, or None when no overlay is retained.
        """
        return self._stack[-1] if self._stack else None

    @property
    def transcript_window(self) -> WindowEntry | None:
        """Return the retained transcript window, if any.

        Returns:
            Transcript entry or None when no transcript is open.
        """
        for entry in self._stack:
            if entry.kind == "transcript":
                return entry
        return None

    @property
    def transcript_uuid(self) -> str | None:
        """Return the UUID currently shown by the transcript window.

        Returns:
            Viewed UUID, or None when no transcript is retained.
        """
        transcript = self.transcript_window
        return transcript.uuid if transcript is not None else None

    @property
    def transcript_scroll(self) -> int:
        """Return the retained transcript scroll offset.

        Returns:
            Non-negative offset from the live transcript tail.
        """
        transcript = self.transcript_window
        return transcript.scroll if transcript is not None else 0

    @property
    def transcript_is_requested(self) -> bool:
        """Return whether the transcript was opened by a ViewRequest.

        Returns:
            True when closing the transcript must settle a request future.
        """
        transcript = self.transcript_window
        return transcript is not None and isinstance(transcript.request, ViewRequest)

    @property
    def pending_summary(self) -> tuple[int, str | None]:
        """Return queued dialog count and the first waiting request source.

        Returns:
            ``(count, source)`` where source prefers caller agent identity.
        """
        self._drop_finished_queue_items()
        if not self._queue:
            return 0, None
        return len(self._queue), self._request_source(self._queue[0])

    @property
    def is_idle(self) -> bool:
        """Return whether no dialog runner or queued answer request remains.

        Returns:
            True when dialog lifecycle work has settled; a live transcript may remain.
        """
        return not self._runners and not self._queue and self.active_window is None

    def submit(self, request: UiRequest) -> bool:
        """Accept a UI request without blocking the event consumer.

        Args:
            request: Pending dialog or view request emitted by EventBus.

        Returns:
            True when the request was retained or queued; False when already settled.
        """
        if not self._is_pending(request):
            return False
        if self._closed:
            request.cancel()
            return False
        request.future.add_done_callback(lambda _future, item=request: self._on_request_settled(item))
        if isinstance(request, ViewRequest):
            self._open_view_request(request)
            return True
        if not isinstance(request, MenuRequest):
            raise TypeError(f"unsupported UI request: {type(request)!r}")
        self._queue.append(request)
        self._notify()
        self._pump()
        return True

    def open_live_transcript(self, uuid: str) -> bool:
        """Open a no-future transcript window from the live agent list.

        Args:
            uuid: Subagent UUID to display.

        Returns:
            True when a transcript window was opened.
        """
        if self._closed:
            return False
        self._replace_transcript(WindowEntry("transcript", None, uuid=uuid))
        return True

    def close_transcript(self) -> bool:
        """Close the transcript and settle its optional view request.

        Returns:
            True when a transcript was present and removed.
        """
        transcript = self.transcript_window
        if transcript is None:
            return False
        self._stack.remove(transcript)
        self._notify()
        request = transcript.request
        if isinstance(request, ViewRequest):
            request.complete("")
        return True

    def set_transcript_scroll(self, scroll: int) -> None:
        """Update the retained transcript scroll offset.

        Args:
            scroll: Desired non-negative offset from the live tail.

        Returns:
            None.
        """
        transcript = self.transcript_window
        if transcript is not None:
            transcript.scroll = max(0, scroll)

    def cancel_all(self) -> bool:
        """Synchronously cancel active, queued, and transcript requests.

        Returns:
            True when any retained window or queue item was cleared.
        """
        retained = [entry.request for entry in self._stack if entry.request is not None]
        queued = list(self._queue)
        runners = tuple(self._runners)
        changed = bool(self._stack or queued or runners)
        self._stack.clear()
        self._queue.clear()
        for runner in runners:
            if not runner.done():
                runner.cancel()
        for request in [*retained, *queued]:
            request.cancel()
        self._notify()
        return changed

    async def wait_idle(self) -> None:
        """Wait until all dialog runners have performed their cleanup.

        Returns:
            None.
        """
        while self._runners:
            runners = tuple(self._runners)
            await asyncio.gather(*runners, return_exceptions=True)
            await asyncio.sleep(0)
            self._pump()

    async def close(self) -> None:
        """Reject new requests, cancel retained work, and await runner cleanup.

        Returns:
            None.
        """
        self._closed = True
        self.cancel_all()
        await self.wait_idle()

    def reload(self) -> bool:
        """Clear an idle live transcript before session state is reset.

        Returns:
            True when idle state was cleared; False when a dialog still owns cleanup.
        """
        if not self.is_idle:
            return False
        return self.close_transcript()

    def _open_view_request(self, request: ViewRequest) -> None:
        """Retain a ViewRequest below any active dialog.

        Args:
            request: Pending read-only view request.

        Returns:
            None.
        """
        uuid = getattr(request, "uuid", None)
        self._replace_transcript(
            WindowEntry("transcript", request, uuid=uuid),
        )

    def _replace_transcript(self, entry: WindowEntry) -> None:
        """Replace the sole transcript entry while preserving a dialog above it.

        Args:
            entry: New transcript entry to retain.

        Returns:
            None.
        """
        previous = self.transcript_window
        if previous is not None:
            self._stack.remove(previous)
            if isinstance(previous.request, ViewRequest):
                previous.request.cancel()
        active = self.active_window
        if active is None or active.kind == "input":
            self._stack.append(entry)
        else:
            self._stack.insert(self._stack.index(active), entry)
        self._notify()

    def _pump(self) -> None:
        """Start the next pending dialog when no dialog currently owns the keyboard.

        Returns:
            None.
        """
        if self._closed or self.active_window is not None or self._runners:
            return
        self._drop_finished_queue_items()
        if not self._queue:
            return
        request = self._queue.popleft()
        entry = WindowEntry(self._dialog_kind(request), request)
        self._stack.append(entry)
        runner = asyncio.create_task(self._drive_dialog(entry))
        self._runner = runner
        self._runners.add(runner)
        runner.add_done_callback(
            lambda completed, item=entry: self._forget_runner(completed, item),
        )
        self._notify()

    async def _drive_dialog(self, entry: WindowEntry) -> DialogOutcome:
        """Run one started dialog and return its terminal outcome.

        Args:
            entry: Active dialog stack entry.

        Returns:
            Outcome consumed by the task done callback.
        """
        request = entry.request
        assert isinstance(request, MenuRequest)
        try:
            return DialogOutcome(answer=await self._run_dialog(request))
        except asyncio.CancelledError:
            return DialogOutcome(cancelled=True)
        except (EOFError, KeyboardInterrupt):
            return DialogOutcome(cancelled=True)
        except BaseException as exc:
            return DialogOutcome(failure=exc)

    def _on_request_settled(self, request: UiRequest) -> None:
        """Remove a request externally settled by its EventBus caller.

        Args:
            request: Request whose future completed, failed, or was cancelled.

        Returns:
            None.
        """
        active = self.active_window
        if active is not None and active.request is request:
            if self._runner is not None and not self._runner.done():
                self._runner.cancel()
            return
        transcript = self.transcript_window
        if transcript is not None and transcript.request is request:
            self._stack.remove(transcript)
            self._notify()
            return
        try:
            self._queue.remove(request)  # type: ignore[arg-type]
        except ValueError:
            return
        self._notify()
        self._pump()

    def _forget_runner(
        self,
        runner: asyncio.Task[DialogOutcome],
        entry: WindowEntry,
    ) -> None:
        """Settle one terminal runner, release its window, and promote FIFO work.

        Args:
            runner: Finished dialog runner task.
            entry: Dialog entry created for the finished runner.

        Returns:
            None.
        """
        self._runners.discard(runner)
        if self._runner is runner:
            self._runner = None
        if entry in self._stack:
            self._stack.remove(entry)
        request = entry.request
        if isinstance(request, MenuRequest):
            if runner.cancelled():
                request.cancel()
            else:
                outcome = runner.result()
                if outcome.cancelled:
                    request.cancel()
                elif outcome.failure is not None:
                    request.fail(outcome.failure)
                else:
                    request.complete(outcome.answer)
        self._notify()
        self._pump()

    def _drop_finished_queue_items(self) -> None:
        """Discard queued requests whose callers no longer wait for a result.

        Returns:
            None.
        """
        if self._queue:
            self._queue = deque(request for request in self._queue if self._is_pending(request))

    def _is_pending(self, request: UiRequest) -> bool:
        """Return whether a request has an unfinished public future.

        Args:
            request: Request to inspect.

        Returns:
            True only when the request can still be settled.
        """
        return request.future is not None and not request.future.done()

    def _request_source(self, request: UiRequest) -> str:
        """Format the request source for queue status.

        Args:
            request: Request carrying optional caller identity and source.

        Returns:
            Caller agent type when present, otherwise event source.
        """
        return request.caller_agent_type or request.source

    def _dialog_kind(self, request: MenuRequest) -> str:
        """Map a dialog event type to its rendering and key-routing kind.

        Args:
            request: Active answer request.

        Returns:
            Stable lowercase dialog kind.
        """
        return request.type.removesuffix("_menu")

    def _notify(self) -> None:
        """Notify the owning controller that derived rendering state changed.

        Returns:
            None.
        """
        self._on_change()
