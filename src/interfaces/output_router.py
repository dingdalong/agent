"""Route events after synchronously updating the shared agent view store."""

from __future__ import annotations

from dataclasses import replace as replace_dataclass

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
    ResponseDelta,
    ThinkingDelta,
    InteractionCompleted,
    LLMCallCompleted,
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
from src.common.session_state import SessionState


class OutputRouter:
    """Keep agent state coherent while isolating TTY foreground output."""

    def __init__(
        self,
        ui: UserInterface,
        store: AgentViewStore,
        passthrough: bool = False,
        session_state: SessionState | None = None,
        data_guard: object | None = None,
    ) -> None:
        """Initialize an event router.

        Args:
            ui: User interface receiving visible events.
            store: Shared agent/session UI read model.
            passthrough: Whether body events should remain visible for plain output.
            session_state: Session state receiving persisted view records.
            data_guard: Shared sensitive-data guard applied at the redaction boundary.

        Returns:
            None.
        """
        self.ui = ui
        self.store = store
        self.passthrough = passthrough
        self.session_state = session_state
        self.data_guard = data_guard

    def _redact(self, event: Event) -> Event:
        """在文本进入展示、状态层与磁盘之前完成统一脱敏。

        流式正文是唯一由模型实时产出、又要同时进入前台展示与会话日志的文本，
        因此脱敏必须发生在消费端入口，使「显示 / 内存 / 磁盘 / 下一轮请求」共用
        同一份脱敏文本。已在入口脱敏的消息不受影响（脱敏本身幂等）。

        Args:
            event: 事件总线投递的事件。

        Returns:
            脱敏后的等价事件；无需改写时返回原对象。
        """
        guard = self.data_guard
        if guard is None or not isinstance(event, _TEXT_DELTA_EVENTS):
            return event
        content = getattr(event, "content", "")
        if not isinstance(content, str) or not content:
            return event
        safe = guard.redact(content)
        if not isinstance(safe, str) or safe == content:
            return event
        return replace_dataclass(event, content=safe)

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
        event = self._redact(event)
        self.store.record(event)
        if self.session_state is not None and isinstance(event, _LLM_ACCOUNTING_EVENTS):
            self.session_state.record_llm_event(event)

        if (
            self.session_state is not None
            and isinstance(event, SubagentLifecycle)
            and event.phase == "end"
        ):
            snapshot = self.store.export_subagent(event.agent_uuid)
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

_TEXT_DELTA_EVENTS = (ResponseDelta, ThinkingDelta, CompactDelta)
_LLM_BOUNDARY_EVENTS = (LLMCallStarted, LLMRetrying, LLMLengthRetrying, LLMCallFailed)
_LLM_ACCOUNTING_EVENTS = (LLMCallStarted, LLMCallCompleted, LLMCallFailed)
