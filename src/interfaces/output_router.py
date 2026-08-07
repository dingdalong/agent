"""Route events after synchronously updating the shared agent view store."""

from __future__ import annotations

from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    InputMenu,
    ModelMenu,
    PermissionMenu,
    TranscriptView,
)
from src.events.types import (
    CompactDelta,
    Event,
    InteractionCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    OutputRequested,
    PermissionNotice,
    SubagentLifecycle,
    TaskStateChanged,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.base import UserInterface
from src.mgr.session_state import SessionState


class OutputRouter:
    """Keep agent state coherent while isolating TTY foreground output."""

    def __init__(
        self,
        ui: UserInterface,
        store: AgentViewStore,
        passthrough: bool = False,
        session_state: SessionState | None = None,
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
        self.session_state = session_state

    def bind_session_state(self, state: SessionState) -> None:
        """切换当前前台会话的可见事件写入目标。"""
        self.session_state = state

    async def dispatch(self, event: Event) -> None:
        """Record an event, then decide whether it reaches the frontend.

        Args:
            event: Event consumed from the application event bus.

        Returns:
            None.
        """
        self.store.record(event)

        if self.session_state is not None:
            agent_uuid = getattr(event, "agent_uuid", None)
            if not isinstance(agent_uuid, str) or not agent_uuid:
                agent_uuid = getattr(event, "caller_uuid", None)
            foreground_uuid = self.store.foreground_uuid
            if agent_uuid and agent_uuid != foreground_uuid:
                snapshot = self.store.export_subagent(agent_uuid)
                if snapshot is not None:
                    self.session_state.record_subagent_snapshot(snapshot)

        if isinstance(event, (SubagentLifecycle, TaskStateChanged)):
            return

        if isinstance(event, _LLM_BOUNDARY_EVENTS):
            foreground_uuid = self.store.foreground_uuid
            if foreground_uuid is not None and event.caller_uuid == foreground_uuid:
                if isinstance(event, LLMCallStarted):
                    self.store.flush_completed()
                if self.session_state is not None:
                    self.session_state.record_event(event)
                await self.ui.on_event(event)
            return

        if self.passthrough:
            if self.session_state is not None:
                self.session_state.record_event(event)
            await self.ui.on_event(event)
            return

        if isinstance(event, _CONTROL_EVENTS):
            if self.session_state is not None:
                self.session_state.record_event(event)
            await self.ui.on_event(event)
            return

        if isinstance(event, CompactDelta):
            if self.session_state is not None:
                self.session_state.record_event(event)
            await self.ui.on_event(event)
            return

        if self._is_background(event):
            return

        if self.session_state is not None:
            self.session_state.record_event(event)
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
    ModelMenu,
    PermissionMenu,
    TranscriptView,
    PermissionNotice,
    OutputRequested,
    InteractionCompleted,
)

_LLM_BOUNDARY_EVENTS = (LLMCallStarted, LLMRetrying, LLMLengthRetrying, LLMCallFailed)
