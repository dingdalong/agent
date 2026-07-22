"""Single read model for agent/session status and transcripts."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from src.events.types import (
    Event,
    LLMCallCompleted,
    LLMCallStarted,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Cumulative token usage for one scope."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """Latest exact context use and its known window limit."""

    used_tokens: int = 0
    limit_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """Immutable UI snapshot of one agent."""

    uuid: str
    agent_type: str
    is_main: bool
    running: bool
    usage: TokenUsage
    context: ContextUsage
    elapsed_seconds: float
    activity: str = ""


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Immutable UI snapshot of session totals and foreground context."""

    usage: TokenUsage
    foreground_context: ContextUsage


@dataclass(slots=True)
class _AgentState:
    """Mutable state backing one immutable agent snapshot."""

    agent_type: str
    is_main: bool
    running: bool = True
    usage: TokenUsage = field(default_factory=TokenUsage)
    context: ContextUsage = field(default_factory=ContextUsage)
    started_monotonic: float | None = None
    ended_monotonic: float | None = None
    transcript: deque[tuple[str, str]] = field(default_factory=deque)
    messages: list[dict] | None = None
    activity: str = ""  # 该 agent 的最新活动文案（等待响应/思考中/回应中/工具名），驱动底部列表实时显示


class AgentViewStore:
    """Own all UI-facing agent lifecycle, usage, context, and transcript state."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        history_limit: int = 50,
        transcript_limit: int = 400,
    ) -> None:
        """Initialize an empty session read model.

        Args:
            clock: Monotonic clock used to calculate agent elapsed time.
            history_limit: Maximum completed subagent views retained.
            transcript_limit: Maximum transcript segments retained per agent.

        Returns:
            None.
        """
        self._clock = clock
        self._history_limit = max(0, history_limit)
        self._transcript_limit = max(1, transcript_limit)
        self._foreground_uuid: str | None = None
        self._session_usage = TokenUsage()
        self._active: dict[str, _AgentState] = {}
        self._history: dict[str, _AgentState] = {}

    @property
    def foreground_uuid(self) -> str | None:
        """Return the currently registered foreground UUID.

        Returns:
            Foreground agent UUID, or None before registration.
        """
        return self._foreground_uuid

    def register_foreground(self, uuid: str, agent_type: str = "agent") -> None:
        """Register the root agent represented by the session status bar.

        Args:
            uuid: Root agent UUID.
            agent_type: Root agent type label.

        Returns:
            None.
        """
        if not uuid:
            return
        self._foreground_uuid = uuid
        existing = self._active.get(uuid)
        if existing is None:
            self._active[uuid] = self._new_state(
                agent_type=agent_type,
                is_main=True,
                started=self._clock(),
            )
            return
        existing.agent_type = agent_type
        existing.is_main = True

    def record(self, event: Event) -> None:
        """Apply one event to the read model.

        Args:
            event: Event carrying lifecycle, usage, context, or transcript data.

        Returns:
            None.
        """
        if isinstance(event, LLMCallCompleted):
            self._record_completion(event)
        elif isinstance(event, LLMCallStarted):
            self._record_call_start(event)
        elif isinstance(event, SubagentLifecycle):
            self._record_lifecycle(event)
        elif isinstance(event, ResponseDelta):
            self._append_stream(event, "response", event.content)
        elif isinstance(event, ThinkingDelta):
            self._append_stream(event, "thinking", event.content)
        elif isinstance(event, ToolCallStarted):
            self._append_tool_start(event)
        elif isinstance(event, ToolCallCompleted):
            self._append_tool_completion(event)

    def flush_completed(self) -> None:
        """Move completed subagents into bounded history storage.

        Returns:
            None.
        """
        for uuid, state in list(self._active.items()):
            if not state.is_main and not state.running:
                self._history[uuid] = state
                del self._active[uuid]
        excess = len(self._history) - self._history_limit
        if excess <= 0:
            return
        ordered = sorted(
            self._history.items(),
            key=lambda item: item[1].ended_monotonic
            if item[1].ended_monotonic is not None
            else float("-inf"),
        )
        for uuid, _ in ordered[:excess]:
            del self._history[uuid]

    def session_snapshot(self) -> SessionSnapshot:
        """Return session totals and the foreground agent's latest context.

        Returns:
            Immutable session snapshot.
        """
        foreground = self._find(self._foreground_uuid)
        context = foreground.context if foreground is not None else ContextUsage()
        return SessionSnapshot(self._session_usage, context)

    def agent_snapshot(self, uuid: str) -> AgentSnapshot | None:
        """Return one agent snapshot by UUID.

        Args:
            uuid: Target agent UUID.

        Returns:
            Immutable snapshot, or None when the retained view is absent.
        """
        state = self._find(uuid)
        if state is None:
            return None
        return self._snapshot(uuid, state)

    def active_agent_snapshots(self) -> list[AgentSnapshot]:
        """Return live-panel snapshots with the main agent first.

        Returns:
            Active agent snapshots in stable display order.
        """
        snapshots = [
            self._snapshot(uuid, state)
            for uuid, state in self._active.items()
        ]
        return sorted(snapshots, key=lambda item: not item.is_main)

    def subagent_snapshots(self) -> list[AgentSnapshot]:
        """Return retained history followed by active subagents.

        Returns:
            All non-main snapshots available to the agent browser.
        """
        snapshots = [
            self._snapshot(uuid, state)
            for uuid, state in self._history.items()
        ]
        snapshots.extend(
            self._snapshot(uuid, state)
            for uuid, state in self._active.items()
            if not state.is_main
        )
        return snapshots

    def transcript_segments(self, uuid: str) -> list[tuple[str, str]]:
        """Return a copy of one agent's incremental transcript segments.

        Args:
            uuid: Target agent UUID.

        Returns:
            Ordered ``(kind, text)`` transcript segments.
        """
        state = self._find(uuid)
        return list(state.transcript) if state is not None else []

    def transcript_messages(self, uuid: str) -> list[dict]:
        """Return a copy of one completed agent's raw message snapshot.

        Args:
            uuid: Target agent UUID.

        Returns:
            Raw message dictionaries, or an empty list when unavailable.
        """
        state = self._find(uuid)
        if state is None or state.messages is None:
            return []
        return list(state.messages)

    def reset(self) -> None:
        """Clear all session usage, foreground, agent, and transcript state.

        Returns:
            None.
        """
        self._foreground_uuid = None
        self._session_usage = TokenUsage()
        self._active.clear()
        self._history.clear()

    def _new_state(
        self,
        agent_type: str,
        is_main: bool,
        started: float | None = None,
    ) -> _AgentState:
        """Create mutable state with the configured transcript bound.

        Args:
            agent_type: Agent type label.
            is_main: Whether the agent is the foreground root.
            started: Optional monotonic lifecycle start.

        Returns:
            Initialized mutable state.
        """
        return _AgentState(
            agent_type=agent_type,
            is_main=is_main,
            started_monotonic=started,
            transcript=deque(maxlen=self._transcript_limit),
        )

    def _find(self, uuid: str | None) -> _AgentState | None:
        """Find active or historical state by UUID.

        Args:
            uuid: Target UUID, or None for an unidentified event.

        Returns:
            Mutable retained state, or None.
        """
        if uuid is None:
            return None
        return self._active.get(uuid) or self._history.get(uuid)

    def _ensure_event_state(self, event: Event) -> _AgentState | None:
        """Find or defensively create state for an identified agent event.

        Args:
            event: Event carrying caller identity（caller_uuid/caller_agent_type 为 Event 基类字段）。

        Returns:
            Mutable state, or None for an event without UUID.
        """
        uuid = event.caller_uuid
        if not uuid:
            return None
        existing = self._find(uuid)
        if existing is not None:
            return existing
        agent_type = event.caller_agent_type or "?"
        state = self._new_state(
            agent_type=agent_type,
            is_main=uuid == self._foreground_uuid,
        )
        self._active[uuid] = state
        return state

    def _record_completion(self, event: LLMCallCompleted) -> None:
        """Accumulate session and identified-agent usage from one call.

        Args:
            event: Completed LLM call usage event.

        Returns:
            None.
        """
        delta = TokenUsage(
            max(0, event.input_tokens or 0),
            max(0, event.output_tokens or 0),
            max(0, event.cache_read_input_tokens or 0),
        )
        self._session_usage = self._add_usage(self._session_usage, delta)
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.usage = self._add_usage(state.usage, delta)
        if event.input_tokens is not None:
            state.context = ContextUsage(
                used_tokens=max(0, event.input_tokens),
                limit_tokens=state.context.limit_tokens,
            )

    def _record_call_start(self, event: LLMCallStarted) -> None:
        """Record a known context limit and mark the calling agent as awaiting a response.

        Args:
            event: LLM call start event.

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.activity = "等待响应"
        if event.context_limit <= 0:
            return
        state.context = ContextUsage(
            used_tokens=state.context.used_tokens,
            limit_tokens=event.context_limit,
        )

    def _record_lifecycle(self, event: SubagentLifecycle) -> None:
        """Apply start/end lifecycle information in an order-stable manner.

        Args:
            event: Subagent lifecycle event.

        Returns:
            None.
        """
        uuid = event.agent_uuid
        if not uuid:
            return
        state = self._find(uuid)
        if event.phase == "start":
            if state is not None and not state.running:
                return
            if state is None:
                state = self._new_state(
                    agent_type=event.agent_type or "?",
                    is_main=False,
                    started=self._clock(),
                )
                self._active[uuid] = state
            else:
                state.agent_type = event.agent_type or state.agent_type
                if state.started_monotonic is None:
                    state.started_monotonic = self._clock()
            return
        if state is None:
            state = self._new_state(
                agent_type=event.agent_type or "?",
                is_main=False,
            )
            self._active[uuid] = state
        state.agent_type = event.agent_type or state.agent_type
        state.running = False
        if state.ended_monotonic is None:
            state.ended_monotonic = self._clock()
        if event.messages is not None:
            state.messages = list(event.messages)

    def _append_stream(self, event: Event, kind: str, content: str) -> None:
        """Append or merge one streaming transcript delta.

        Args:
            event: Identified streaming event.
            kind: Transcript segment kind.
            content: Delta text appended when nonempty.

        Returns:
            None.
        """
        if not content:
            return
        state = self._ensure_event_state(event)
        if state is None:
            return
        if kind == "response":
            state.activity = "回应中"  # ThinkingDelta 为 DETAIL 级默认丢弃，故只据 response 更新
        if state.transcript and state.transcript[-1][0] == kind:
            previous_kind, previous_text = state.transcript[-1]
            state.transcript[-1] = (previous_kind, previous_text + content)
        else:
            state.transcript.append((kind, content))

    def _append_tool_start(self, event: ToolCallStarted) -> None:
        """Append a tool-start transcript line.

        Args:
            event: Tool-start event with name and optional detail.

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.activity = event.tool_name
        detail = event.detail.strip()
        suffix = f" {detail}" if detail else ""
        state.transcript.append(("tool", f"● {event.tool_name}{suffix}\n"))

    def _append_tool_completion(self, event: ToolCallCompleted) -> None:
        """Append a compact tool-completion transcript line.

        Args:
            event: Tool-completion event with preview and duration.

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        preview_lines = (event.result_preview or "").strip().splitlines()
        fallback = "完成" if event.status == "success" else "失败"
        first = preview_lines[0] if preview_lines else fallback
        state.transcript.append((
            "tool",
            f"  ⎿ {first}  ({event.duration_seconds:.2f}s)\n",
        ))

    def _snapshot(self, uuid: str, state: _AgentState) -> AgentSnapshot:
        """Freeze one mutable state for presentation.

        Args:
            uuid: Agent UUID associated with the state.
            state: Mutable retained agent state.

        Returns:
            Immutable agent snapshot with calculated elapsed time.
        """
        elapsed = 0.0
        if state.started_monotonic is not None:
            end = self._clock() if state.running else state.ended_monotonic
            if end is not None:
                elapsed = max(0.0, end - state.started_monotonic)
        return AgentSnapshot(
            uuid=uuid,
            agent_type=state.agent_type,
            is_main=state.is_main,
            running=state.running,
            usage=state.usage,
            context=state.context,
            elapsed_seconds=elapsed,
            activity=state.activity,
        )

    @staticmethod
    def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
        """Add two immutable token usage values.

        Args:
            left: Existing cumulative usage.
            right: Nonnegative usage delta.

        Returns:
            New cumulative token usage.
        """
        return TokenUsage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        )
