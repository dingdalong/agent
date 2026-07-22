"""Route events after synchronously updating the shared agent view store."""

from __future__ import annotations

from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    InputMenu,
    PermissionMenu,
    TranscriptView,
)
from src.events.types import (
    CompactDelta,
    Event,
    LLMCallFailed,
    LLMCallStarted,
    LLMRetrying,
    OutputRequested,
    PermissionNotice,
    SubagentLifecycle,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.base import UserInterface


class OutputRouter:
    """Keep agent state coherent while isolating TTY foreground output."""

    def __init__(
        self,
        ui: UserInterface,
        store: AgentViewStore,
        passthrough: bool = False,
    ) -> None:
        """Initialize an event router.

        Args:
            ui: User interface receiving visible events.
            store: Shared agent/session UI read model.
            passthrough: Whether body events should remain visible for plain output.

        Returns:
            None.
        """
        self.ui = ui
        self.store = store
        self.passthrough = passthrough

    async def dispatch(self, event: Event) -> None:
        """Record an event, then decide whether it reaches the frontend.

        Args:
            event: Event consumed from the application event bus.

        Returns:
            None.
        """
        self.store.record(event)

        if isinstance(event, SubagentLifecycle):
            return

        if isinstance(event, _LLM_BOUNDARY_EVENTS):
            foreground_uuid = self.store.foreground_uuid
            if foreground_uuid is not None and event.caller_uuid == foreground_uuid:
                if isinstance(event, LLMCallStarted):
                    self.store.flush_completed()
                await self.ui.on_event(event)
            return

        if self.passthrough:
            await self.ui.on_event(event)
            return

        if isinstance(event, _CONTROL_EVENTS):
            await self.ui.on_event(event)
            return

        if isinstance(event, CompactDelta):
            await self.ui.on_event(event)
            return

        if self._is_background(event):
            return

        await self.ui.on_event(event)

    def _is_background(self, event: Event) -> bool:
        """Return whether an identified event belongs to a non-foreground agent.

        Args:
            event: Event carrying a caller UUID (caller_uuid 现为 Event 基类一等属性)。

        Returns:
            True only for events identified as a different agent.
        """
        return event.caller_uuid is not None and event.caller_uuid != self.store.foreground_uuid


_CONTROL_EVENTS = (
    InputMenu,
    ChoiceMenu,
    ChoiceInputMenu,
    FormMenu,
    PermissionMenu,
    TranscriptView,
    PermissionNotice,
    OutputRequested,
)

_LLM_BOUNDARY_EVENTS = (LLMCallStarted, LLMRetrying, LLMCallFailed)
