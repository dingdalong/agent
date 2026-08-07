"""Session status, task, and subagent transcript read model."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from src.events.types import (
    Event,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    ResponseDelta,
    SubagentLifecycle,
    TaskStateChanged,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)

# 截断阶段分类到转录用中文标签的映射。
_TRUNCATION_KIND_LABELS = {
    "tool_call": "工具调用",
    "content": "正文",
    "thinking": "思考",
    "unknown": "未知",
}


def _nonnegative_int(value: object) -> int:
    return max(0, value) if isinstance(value, int) else 0


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
    task: str = ""


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
    active_tools: dict[str, str] = field(default_factory=dict)  # 在飞工具 {tool_call_id: tool_name}，用于并发下判断本轮工具是否全部完成、正确复位状态词
    task: str = ""  # 委派时的任务摘要，供子 agent 状态行展示


class AgentViewStore:
    """Own transient lifecycle/status data and subagent transcripts.

    Foreground chat history belongs to SessionState; main-agent events update activity,
    usage and tool status here without creating a second transcript copy.
    """

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
        self._task_snapshot: list[dict] = []

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
        elif isinstance(event, LLMRetrying):
            self._record_retry(event)
        elif isinstance(event, LLMLengthRetrying):
            self._record_length_retry(event)
        elif isinstance(event, LLMCallFailed):
            self._record_failure(event)
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
        elif isinstance(event, TaskStateChanged):
            self._task_snapshot = list(event.tasks)

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

    def task_snapshot(self) -> list[dict]:
        """返回最新的任务列表快照，供 TUI 渲染任务进度面板。

        Returns:
            最新任务摘要列表的副本。
        """
        return list(self._task_snapshot)

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

    def has_subagents(self) -> bool:
        """Return whether any non-main agents exist in history or active.

        Returns:
            True when at least one subagent is retained.
        """
        if self._history:
            return True
        return any(not state.is_main for state in self._active.values())

    def has_running_subagents(self) -> bool:
        """Return whether any non-main agents are currently running.

        Returns:
            True when at least one running subagent exists.
        """
        return any(
            not state.is_main and state.running
            for state in self._active.values()
        )

    def transcript_segments(self, uuid: str) -> list[tuple[str, str]]:
        """Return a copy of one agent's incremental transcript segments.

        Args:
            uuid: Target agent UUID.

        Returns:
            Ordered ``(kind, text)`` transcript segments.
        """
        state = self._find(uuid)
        return list(state.transcript) if state is not None else []

    def transcript_tail(self, uuid: str) -> tuple[int, tuple[str, str] | None]:
        """Return segment count and the last segment without copying the deque.

        Args:
            uuid: Target agent UUID.

        Returns:
            ``(count, last_segment)`` or ``(0, None)`` when empty/absent.
        """
        state = self._find(uuid)
        if state is None or not state.transcript:
            return (0, None)
        return (len(state.transcript), state.transcript[-1])

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

    def export_subagent(self, uuid: str) -> dict | None:
        """Return a JSON-safe snapshot suitable for SessionState persistence."""
        state = self._find(uuid)
        if state is None or state.is_main:
            return None
        snapshot = self._snapshot(uuid, state)
        return {
            "uuid": snapshot.uuid,
            "agent_type": snapshot.agent_type,
            "running": snapshot.running,
            "usage": {
                "input_tokens": snapshot.usage.input_tokens,
                "output_tokens": snapshot.usage.output_tokens,
                "cache_read_tokens": snapshot.usage.cache_read_tokens,
            },
            "context": {
                "used_tokens": snapshot.context.used_tokens,
                "limit_tokens": snapshot.context.limit_tokens,
            },
            "elapsed_seconds": snapshot.elapsed_seconds,
            "activity": snapshot.activity,
            "task": snapshot.task,
            "transcript": [
                {"kind": kind, "text": text}
                for kind, text in state.transcript
            ],
            "messages": list(state.messages) if state.messages is not None else [],
        }

    def restore_subagents(self, snapshots: list[dict]) -> None:
        """Hydrate completed, read-only subagent views from a session snapshot."""
        for uuid in [
            key for key, state in self._active.items() if not state.is_main
        ]:
            del self._active[uuid]
        self._history.clear()

        now = self._clock()
        for snapshot in snapshots[-self._history_limit:] if self._history_limit else []:
            uuid = snapshot.get("uuid")
            agent_type = snapshot.get("agent_type")
            if not isinstance(uuid, str) or not uuid or not isinstance(agent_type, str):
                continue
            state = self._new_state(agent_type=agent_type, is_main=False)
            state.running = False
            elapsed = snapshot.get("elapsed_seconds", 0.0)
            if not isinstance(elapsed, (int, float)):
                elapsed = 0.0
            state.started_monotonic = now - max(0.0, float(elapsed))
            state.ended_monotonic = now
            usage = snapshot.get("usage", {})
            context = snapshot.get("context", {})
            state.usage = TokenUsage(
                input_tokens=_nonnegative_int(usage.get("input_tokens", 0))
                if isinstance(usage, dict) else 0,
                output_tokens=_nonnegative_int(usage.get("output_tokens", 0))
                if isinstance(usage, dict) else 0,
                cache_read_tokens=_nonnegative_int(usage.get("cache_read_tokens", 0))
                if isinstance(usage, dict) else 0,
            )
            state.context = ContextUsage(
                used_tokens=_nonnegative_int(context.get("used_tokens", 0))
                if isinstance(context, dict) else 0,
                limit_tokens=_nonnegative_int(context.get("limit_tokens", 0))
                if isinstance(context, dict) else 0,
            )
            activity = snapshot.get("activity", "")
            task = snapshot.get("task", "")
            state.activity = activity if isinstance(activity, str) else ""
            state.task = task if isinstance(task, str) else ""
            transcript = snapshot.get("transcript", [])
            if isinstance(transcript, list):
                for segment in transcript:
                    if not isinstance(segment, dict):
                        continue
                    kind = segment.get("kind")
                    text = segment.get("text")
                    if isinstance(kind, str) and isinstance(text, str):
                        state.transcript.append((kind, text))
            messages = snapshot.get("messages", [])
            state.messages = list(messages) if isinstance(messages, list) else []
            self._history[uuid] = state

    def reset(self) -> None:
        """Clear all session usage, foreground, agent, and transcript state.

        Returns:
            None.
        """
        self._foreground_uuid = None
        self._session_usage = TokenUsage()
        self._active.clear()
        self._history.clear()
        self._task_snapshot = []

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
        state.active_tools.clear()  # 新一轮 LLM 调用意味着上一轮工具从视图看已结束，清掉可能残留的在飞条目（如取消路径只发 start 未发 completed）
        state.activity = (
            f"等待响应 {event.attempt}/{event.max_attempts}"
            if event.attempt > 1 and event.max_attempts > 0
            else "等待响应"
        )
        if event.context_limit <= 0:
            return
        state.context = ContextUsage(
            used_tokens=state.context.used_tokens,
            limit_tokens=event.context_limit,
        )

    def _record_retry(self, event: LLMRetrying) -> None:
        """记录一次安全重试边界并阻断前后正文分段合并。

        Args:
            event: 携带安全错误类别、摘要与残片状态的重试事件。

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.activity = "重试中"
        if state.is_main:
            return
        state.transcript.append((
            "retry",
            f"⚠ 尝试 {event.attempt}/{event.max_attempts} 失败，将重试 "
            f"[{event.error_kind}] {event.safe_message} "
            f"(partial={str(event.partial).lower()}, tool={event.tool_fragment_state})\n",
        ))

    def _record_length_retry(self, event: LLMLengthRetrying) -> None:
        """记录一次输出长度截断的自动恢复并阻断前后正文分段合并。

        重生成路径会丢弃被截断的思考/正文流，追加一个 retry 段可隔断被丢弃流与
        重生成流在转录中被误合并为一段。

        Args:
            event: 携带截断阶段、恢复策略与推理力度的长度恢复事件。

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.activity = "恢复中"
        if state.is_main:
            return
        kind_label = _TRUNCATION_KIND_LABELS.get(event.truncation_kind, event.truncation_kind)
        if event.strategy == "regenerate-lower-effort":
            action = f"降低推理力度至 {event.effort} 后重生成"
        elif event.strategy == "regenerate-compress":
            action = "压缩思考后重生成"
        else:
            action = "从中断处继续生成"
        state.transcript.append((
            "retry",
            f"⚠ 输出截断（{kind_label}）：{action} "
            f"({event.attempt}/{event.max_attempts})\n",
        ))

    def _record_failure(self, event: LLMCallFailed) -> None:
        """记录一次安全终态错误并把 agent 活动更新为失败。

        Args:
            event: 携带安全错误与诊断关联字段的终态失败事件。

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.activity = "失败"
        if state.is_main:
            return
        metadata = [
            f"attempts={event.attempts}",
            f"partial={str(event.partial).lower()}",
            f"tool={event.tool_fragment_state}",
        ]
        if event.status_code is not None:
            metadata.append(f"status={event.status_code}")
        if event.provider_code:
            metadata.append(f"code={event.provider_code}")
        if event.request_id:
            metadata.append(f"request_id={event.request_id}")
        if event.diagnostic_id:
            metadata.append(f"diagnostic_id={event.diagnostic_id}")
        state.transcript.append((
            "error",
            f"✘ LLM 调用失败 [{event.error_kind}] {event.safe_message} "
            f"({', '.join(metadata)})\n",
        ))

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
            if event.task:
                state.task = event.task
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
        state.activity = "回应中" if kind == "response" else "思考中"  # 按增量种类切换面板状态词：thinking→思考中、response→回应中
        if state.is_main:
            return
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
        state.active_tools[event.tool_call_id] = event.tool_name
        state.activity = event.tool_name
        if state.is_main:
            return
        display = event.display
        if display is not None and hasattr(display, "title"):
            content = (display.content or "").strip()
            suffix = f" {content.splitlines()[0]}" if content else ""
            state.transcript.append(("tool", f"● {display.title}{suffix}\n"))
        else:
            detail = event.detail.strip()
            suffix = f" {detail}" if detail else ""
            state.transcript.append(("tool", f"● {event.tool_name}{suffix}\n"))

    def _append_tool_completion(self, event: ToolCallCompleted) -> None:
        """移除该在飞工具、据剩余在飞工具复位状态词，并追加紧凑的工具完成转录行。

        Args:
            event: 带结果预览与耗时的工具完成事件。

        Returns:
            None.
        """
        state = self._ensure_event_state(event)
        if state is None:
            return
        state.active_tools.pop(event.tool_call_id, None)
        if state.active_tools:
            # 并发工具尚未全部完成：状态词切到最近仍在运行的工具，避免停留在刚完成的工具名上
            state.activity = next(reversed(state.active_tools.values()))
        else:
            # 本轮工具全部完成：复位为「等待响应」，衔接紧随其后的下一次 LLM 调用
            state.activity = "等待响应"
        if state.is_main:
            return
        display = event.display
        if display is not None and hasattr(display, "title") and hasattr(display, "content"):
            ok = event.status == "success"
            mark = "✔" if ok else "✘"
            line = f"  {mark} {display.title}  ({event.duration_seconds:.2f}s)\n"
            content = (display.content or "").strip()
            if content:
                # 转录只取前 10 行避免膨胀
                lines = content.splitlines()[:10]
                line += "\n".join(f"  {l}" for l in lines) + "\n"
            state.transcript.append(("tool", line))
        else:
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
            task=state.task,
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
