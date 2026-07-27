"""Thin UserInterface facade over composable Inline UI controllers."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager

from rich.text import Text

from src.events.menu import FormQuestion
from src.events.types import Event
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.base import UserInterface
from src.interfaces.inline.controller import InlineController
from src.interfaces.turn_clock import TurnClock


class InlineInterface(UserInterface):
    """Expose the framework UI contract while delegating terminal behavior."""

    def __init__(
        self,
        agent_view_store: AgentViewStore,
        slash_commands: list[tuple[str, str]] | None = None,
        turn_clock: TurnClock | None = None,
    ) -> None:
        """Initialize the composable Inline UI.

        Args:
            agent_view_store: Shared source for session, agent, and transcript views.
            slash_commands: Slash-command names and descriptions for completion.
            turn_clock: Shared turn clock driving human-wait-aware elapsed time; a
                standalone instance is created when omitted (e.g. in tests).

        Returns:
            None.
        """
        super().__init__()
        self._controller = InlineController(
            agent_view_store,
            slash_commands,
            turn_clock or TurnClock(),
        )
        self.is_tty = self._controller.is_tty

    def __getattr__(self, name: str):
        """Delegate internal rendering/test hooks to the composed controller.

        Args:
            name: Missing facade attribute requested by a caller.

        Returns:
            Matching controller attribute.
        """
        return getattr(self._controller, name)

    def __setattr__(self, name: str, value: object) -> None:
        """Route writes for existing controller fields without duplicating state.

        Args:
            name: Facade attribute being assigned.
            value: New attribute value.

        Returns:
            None.
        """
        controller = self.__dict__.get("_controller")
        if controller is not None and hasattr(controller, name):
            setattr(controller, name, value)
            return
        object.__setattr__(self, name, value)

    def watch_interrupt(
        self,
        request_interrupt: Callable[[], None],
    ) -> AbstractContextManager[None]:
        """Delegate interrupt monitoring to the active controller.

        Args:
            request_interrupt: Callback invoked for a user interrupt.

        Returns:
            Context manager restoring the previous interrupt callback on exit.
        """
        return self._controller.watch_interrupt(request_interrupt)

    async def start(self) -> None:
        """Start the composed frontend.

        Returns:
            None.
        """
        await self._controller.start()

    async def stop(self) -> None:
        """Stop the composed frontend and restore terminal streams.

        Returns:
            None.
        """
        await self._controller.stop()

    def reload(self) -> None:
        """Reset interaction-only UI state for a cleared session.

        Returns:
            None.
        """
        self._controller.reload()

    async def on_event(self, event: Event) -> None:
        """Deliver one routed event to the composed controller.

        Args:
            event: Visible UI event.

        Returns:
            None.
        """
        await self._controller.on_event(event)

    def cancel_active_input(self) -> bool:
        """Cancel the controller's active bus request and interaction future.

        Returns:
            Whether an active bus request was cancelled.
        """
        return self._controller.cancel_active_input()

    async def wait_interactions_idle(self) -> None:
        """Wait until the controller has released asynchronous window runners.

        Returns:
            None.
        """
        await self._controller.wait_interactions_idle()

    def reset_session_interactions(self) -> AbstractAsyncContextManager[None]:
        """Delegate the reset request gate to the event-owning controller.

        Returns:
            Context manager that drains old interactions and rejects late requests.
        """
        return self._controller.reset_session_interactions()

    def set_permission_mode_provider(
        self,
        provider: Callable[[], str] | None,
    ) -> None:
        """Install the explicit root permission-mode provider.

        Args:
            provider: Callable returning the current mode, or None to remove it.

        Returns:
            None.
        """
        self._controller.set_permission_mode_provider(provider)

    def set_permission_mode_toggle_handler(
        self,
        handler: Callable[[], None] | None,
    ) -> None:
        """Install the normal-input Shift+Tab callback.

        Args:
            handler: Permission mode rotation callback, or None to disable it.

        Returns:
            None.
        """
        self._controller.set_permission_mode_toggle_handler(handler)

    def on_permission_mode_changed(self) -> None:
        """Request redraw after the permission mode changes.

        Returns:
            None.
        """
        self._controller.on_permission_mode_changed()

    async def _write(self, message: str | Text, markdown: bool = False) -> None:
        """Write content through the composed output frontend.

        Args:
            message: Plain text, Markdown source, or Rich text.
            markdown: Whether to interpret string messages as Markdown on TTY.

        Returns:
            None.
        """
        await self._controller._write(message, markdown)

    async def _read_input(
        self,
        prompt: str,
        default: str = "",
        markdown: bool = False,
    ) -> str:
        """Read normal user input through the composed controller.

        Args:
            prompt: Input context prompt.
            default: Initial buffer value.
            markdown: Whether prompt context is Markdown.

        Returns:
            Submitted user text.
        """
        return await self._controller._read_input(prompt, default, markdown)

    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
        suggested_rules: list[str] | None = None,
        mcp_server_rule: str | None = None,
    ) -> str:
        """Read one permission decision through the menu controller.

        Args:
            tool_name: Requested tool name.
            detail: Permission detail text.
            suggested_rules: Optional suggested allow rules.
            mcp_server_rule: Optional server-wide MCP rule.

        Returns:
            Permission decision wire value.
        """
        return await self._controller._read_permission(
            tool_name,
            detail,
            suggested_rules,
            mcp_server_rule,
        )

    async def _read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """Read a direction-key choice through the menu controller.

        Args:
            prompt: Menu context.
            options: Value/label choices.
            default_index: Initially selected option.
            markdown: Whether labels are Markdown.

        Returns:
            Selected value or an empty cancellation value.
        """
        return await self._controller._read_choice(
            prompt,
            options,
            default_index,
            markdown,
        )

    async def _read_form(
        self,
        prompt: str,
        questions: list[FormQuestion],
        markdown: bool = False,
    ) -> str:
        """Read a multi-question form through the form controller.

        Args:
            prompt: Form context.
            questions: Ordered form questions.
            markdown: Whether labels are Markdown.

        Returns:
            Form JSON wire payload or empty cancellation value.
        """
        return await self._controller._read_form(prompt, questions, markdown)

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """Read an options-plus-text interaction through the menu controller.

        Args:
            prompt: Interaction context.
            options: Value/label choices.
            descriptions: Optional aligned descriptions.
            input_placeholder: Placeholder for the free-text row.
            default_index: Initially selected row.
            markdown: Whether labels are Markdown.

        Returns:
            Choice-input JSON wire payload or empty cancellation value.
        """
        return await self._controller._read_choice_input(
            prompt,
            options,
            descriptions,
            input_placeholder,
            default_index,
            markdown,
        )

    async def _read_transcript_view(self, uuid: str) -> str:
        """Open a UUID-selected read-only transcript.

        Args:
            uuid: Target agent UUID.

        Returns:
            Empty string after the view closes.
        """
        return await self._controller._read_transcript_view(uuid)
