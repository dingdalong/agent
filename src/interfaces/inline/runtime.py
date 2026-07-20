"""Shared prompt-toolkit runtime state for the Inline UI."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any


class InteractionMode(StrEnum):
    """Mutually exclusive top-level Inline UI interaction modes."""

    PROCESSING = "processing"
    INPUT = "input"
    SELECT = "select"
    FORM = "form"
    CHOICE_INPUT = "choice_input"
    TRANSCRIPT = "transcript"


class InlineRuntime:
    """Own prompt-toolkit objects, focus references, and interaction settlement."""

    def __init__(self) -> None:
        """Initialize an unattached, non-running runtime.

        Returns:
            None.
        """
        self.mode = InteractionMode.PROCESSING
        self.app: Any = None
        self.app_task: asyncio.Task | None = None
        self.buffer: Any = None
        self.layout: Any = None
        self.input_window: Any = None
        self.agent_list_window: Any = None
        self.agent_list_inner: Any = None
        self.stdout_proxy: Any = None
        self.original_stdout: Any = None
        self.original_stderr: Any = None
        self._input_future: asyncio.Future[str] | None = None

    @property
    def app_running(self) -> bool:
        """Return whether an attached prompt-toolkit application is running.

        Returns:
            True when an attached application reports a running state.
        """
        return self.app is not None and self.app.is_running

    @contextmanager
    def interaction(self) -> Iterator[asyncio.Future[str]]:
        """Own the sole Inline UI interaction future for one context.

        Yields:
            Future settled by the active input, menu, form, or transcript flow.

        Raises:
            RuntimeError: If another interaction context still owns a future.
        """
        if self._input_future is not None:
            raise RuntimeError("an Inline UI interaction is already pending")
        future = asyncio.get_running_loop().create_future()
        self._input_future = future
        try:
            yield future
        finally:
            if not future.done():
                future.cancel()
            if self._input_future is future:
                self._input_future = None

    def pending_input_future(self) -> asyncio.Future[str] | None:
        """Return the unfinished interaction future.

        Returns:
            Pending future, or None when absent/already settled.
        """
        future = self._input_future
        return future if future is not None and not future.done() else None

    def resolve_input(self, text: str) -> bool:
        """Resolve the pending interaction future.

        Args:
            text: Interaction result delivered to the waiter.

        Returns:
            True when a pending future was resolved.
        """
        future = self.pending_input_future()
        if future is None:
            return False
        future.set_result(text)
        return True

    def fail_input(self, exc: BaseException) -> bool:
        """Fail the pending interaction future.

        Args:
            exc: Exception delivered to the waiter.

        Returns:
            True when a pending future was failed.
        """
        future = self.pending_input_future()
        if future is None:
            return False
        future.set_exception(exc)
        return True

    def cancel_input(self) -> bool:
        """Cancel the pending interaction future.

        Returns:
            True when a pending future was cancelled.
        """
        future = self.pending_input_future()
        if future is None:
            return False
        future.cancel()
        return True
