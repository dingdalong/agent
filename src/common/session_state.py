"""单一会话状态及其上下文、可见历史和输入回溯投影。

内存中只有一份消息：``records[].model_message`` 与 ``Agent.history`` 的元素是同一
批 dict 对象，任何一方就地修改都立即对另一方生效，不再维护第二份副本。落盘由
``prepare_commit()`` 取稳定增量，成功后 ``ack_commit()``，长文本统一交给
``src.common.session_log`` 的内容池引用化。
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Iterable

from src.common.session_log import LOG_VERSION, text_digest
from src.common.stream_policy import StreamFlushPolicy


_EMPTY_DIGEST = text_digest("")
_STREAM_KEYS = ("content", "thinking")


@dataclass(slots=True)
class _TextChunks:
    """流式文本的追加缓冲，只在读取边界合并。"""

    chunks: list[str] = field(default_factory=list)
    length: int = 0

    def append(self, value: str) -> None:
        if value:
            self.chunks.append(value)
            self.length += len(value)

    def materialize(self) -> str:
        return "".join(self.chunks)


def _strip_rendered_content(display: object) -> None:
    """把可由配方重建的展示正文从持久化数据里去掉。

    Args:
        display: 已转换为普通 dict 的工具展示数据。

    Returns:
        None；就地清空 ``content``。
    """
    if (
        isinstance(display, dict)
        and display.get("render")
        and isinstance(display.get("content"), str)
    ):
        display["content"] = ""


def _strip_derived_preview(data: dict[str, Any]) -> None:
    """把可由工具结果重建的预览从持久化数据里去掉。

    预览与展示正文一样是工具结果的投影：带有展示配方时只保留元信息，正文与
    预览都在渲染时按引用取回，避免同一段文本在日志里存两份。

    Args:
        data: 已转换为普通 dict 的工具事件数据。

    Returns:
        None；就地清空 ``result_preview``。
    """
    display = data.get("display")
    if (
        isinstance(display, dict)
        and display.get("render")
        and isinstance(data.get("result_preview"), str)
    ):
        data["result_preview"] = ""


@dataclass(slots=True)
class ViewPayload:
    """可持久化的前台展示载荷。

    ``kind`` 决定渲染方式；``data`` 只包含 JSON 可编码的数据。流式回应和工具调用
    会按关联 ID 原地合并，因此一次逻辑输出只对应一条记录。
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: object) -> ViewPayload | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        data = value.get("data", {})
        if not isinstance(kind, str) or not kind or not isinstance(data, dict):
            return None
        return cls(kind=kind, data=copy.deepcopy(data))


@dataclass(slots=True)
class SessionRecord:
    """会话中的一个逻辑记录，可同时参与多个投影。"""

    id: str
    timestamp: float
    kind: str
    model_message: dict[str, Any] | None = None
    view: ViewPayload | None = None
    raw_input: str | None = None
    recallable: bool = False
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回可持久化的记录投影。

        ``model_message`` 与视图数据只做浅层转交：调用方（日志编码器）会构建
        新的引用结构，不需要在这里复制一份正文。
        """
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "model_message": self.model_message,
            "view": (
                {"kind": self.view.kind, "data": self.view.data}
                if self.view is not None else None
            ),
            "raw_input": self.raw_input,
            "recallable": self.recallable,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> SessionRecord | None:
        if not isinstance(value, dict):
            return None
        record_id = value.get("id")
        timestamp = value.get("timestamp")
        kind = value.get("kind")
        message = value.get("model_message")
        raw_input = value.get("raw_input")
        recallable = value.get("recallable", False)
        correlation_id = value.get("correlation_id")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(timestamp, (int, float)) or not isinstance(kind, str) or not kind:
            return None
        if message is not None and not isinstance(message, dict):
            return None
        if raw_input is not None and not isinstance(raw_input, str):
            return None
        if not isinstance(recallable, bool):
            return None
        if correlation_id is not None and not isinstance(correlation_id, str):
            return None
        view_value = value.get("view")
        view = ViewPayload.from_dict(view_value) if view_value is not None else None
        if view_value is not None and view is None:
            return None
        return cls(
            id=record_id,
            timestamp=float(timestamp),
            kind=kind,
            model_message=copy.deepcopy(message),
            view=view,
            raw_input=raw_input,
            recallable=recallable,
            correlation_id=correlation_id,
        )


@dataclass(slots=True)
class LLMCallRecord:
    """一次 provider attempt 的持久化核账记录。"""

    call_id: str
    attempt: int
    model: str
    caller_uuid: str | None
    caller_type: str | None
    reasoning_effort: str | None
    phase: str
    started_at: float
    completed_at: float | None = None
    outcome: str = "running"
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> LLMCallRecord | None:
        if not isinstance(value, dict):
            return None
        string_fields = ("call_id", "model", "phase", "outcome")
        nullable_strings = ("caller_uuid", "caller_type", "reasoning_effort")
        token_fields = (
            "input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
        )
        if any(not isinstance(value.get(key), str) for key in string_fields):
            return None
        if not value.get("call_id") or value.get("phase") not in {"plan", "execute"}:
            return None
        if any(value.get(key) is not None and not isinstance(value.get(key), str) for key in nullable_strings):
            return None
        if type(value.get("attempt")) is not int or value["attempt"] < 1:
            return None
        if not isinstance(value.get("started_at"), (int, float)):
            return None
        if value.get("completed_at") is not None and not isinstance(value.get("completed_at"), (int, float)):
            return None
        if any(value.get(key) is not None and type(value.get(key)) is not int for key in token_fields):
            return None
        return cls(
            call_id=value["call_id"], attempt=value["attempt"], model=value["model"],
            caller_uuid=value.get("caller_uuid"), caller_type=value.get("caller_type"),
            reasoning_effort=value.get("reasoning_effort"), phase=value["phase"],
            started_at=float(value["started_at"]),
            completed_at=float(value["completed_at"]) if value.get("completed_at") is not None else None,
            outcome=value["outcome"],
            **{key: value.get(key) for key in token_fields},
        )


@dataclass(slots=True)
class PreparedCommit:
    """稳定的会话提交快照，只有成功写盘后才能确认。"""

    payload: dict[str, Any]
    records: dict[str, dict[str, Any]]
    context_ids: list[str]
    plan: dict[str, Any] | None
    calls: dict[tuple[str, int], dict[str, Any]]
    stream_deltas: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    record_delta_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    stream_state: dict[tuple[str, str], str] = field(default_factory=dict)
    view_keys: dict[str, frozenset[str]] = field(default_factory=dict)
    resync: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SessionState:
    """会话状态权威写入点。

    记录容器同时是内存单例与落盘源：``context_ids`` 定义模型上下文投影顺序，
    ``records`` 保存每条记录的唯一副本。``prepare_commit()`` 返回自上次提交以来的
    稳定增量载荷，写盘成功后才确认，从而把落盘从「整份快照重写」变成「追加一批」。
    """

    records: list[SessionRecord] = field(default_factory=list)
    context_ids: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    _view_streams: dict[tuple[str, str], _TextChunks] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _dirty_records: set[str] = field(default_factory=set, init=False, repr=False)
    _dirty_context: bool = field(default=False, init=False, repr=False)
    _dirty_plan: bool = field(default=False, init=False, repr=False)
    _dirty_calls: set[tuple[str, int]] = field(default_factory=set, init=False, repr=False)
    _committed_context_ids: list[str] = field(default_factory=list, init=False, repr=False)
    _stream_pending: dict[tuple[str, str], list[str]] = field(
        default_factory=dict, init=False, repr=False,
    )
    _committed_records: set[str] = field(default_factory=set, init=False, repr=False)
    _committed_streams: dict[tuple[str, str], str] = field(
        default_factory=dict, init=False, repr=False,
    )
    _committed_view_keys: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False,
    )
    _streamed_records: set[str] = field(default_factory=set, init=False, repr=False)
    _last_checkpoint_at: float = field(default_factory=time.monotonic, init=False, repr=False)
    stream_policy: StreamFlushPolicy = field(
        default_factory=StreamFlushPolicy, repr=False,
    )

    @classmethod
    def from_dict(cls, value: object) -> SessionState | None:
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != LOG_VERSION
        ):
            return None
        raw_records = value.get("records")
        context_ids = value.get("context_ids")
        raw_llm_calls = value.get("llm_calls")
        if not isinstance(raw_records, list) or not isinstance(context_ids, list) or not isinstance(raw_llm_calls, list):
            return None
        records: list[SessionRecord] = []
        ids: set[str] = set()
        for item in raw_records:
            record = SessionRecord.from_dict(item)
            if record is None or record.id in ids:
                return None
            records.append(record)
            ids.add(record.id)
        if (
            not all(isinstance(item, str) and item in ids for item in context_ids)
            or len(context_ids) != len(set(context_ids))
        ):
            return None
        by_id = {record.id: record for record in records}
        if any(by_id[item].model_message is None for item in context_ids):
            return None
        llm_calls = [LLMCallRecord.from_dict(item) for item in raw_llm_calls]
        if any(item is None for item in llm_calls):
            return None
        call_keys = [(item.call_id, item.attempt) for item in llm_calls if item is not None]
        if len(call_keys) != len(set(call_keys)):
            return None
        state = cls(
            records=records,
            context_ids=list(context_ids),
            plan=copy.deepcopy(value.get("plan")) if isinstance(value.get("plan"), dict) else {},
            llm_calls=[item for item in llm_calls if item is not None],
        )
        state._committed_context_ids = list(context_ids)
        state._committed_records = {record.id for record in records}
        for record in records:
            state._remember_committed(record)
        return state

    def to_dict(self) -> dict[str, Any]:
        """返回完整状态载荷；仅用于校验与测试，落盘走 prepare/ack 提交。"""
        self._materialize_all_view_streams()
        return {
            "version": LOG_VERSION,
            "plan": self.plan,
            "llm_calls": [call.to_dict() for call in self.llm_calls],
            "records": [record.to_dict() for record in self.records],
            "context_ids": list(self.context_ids),
        }

    def prepare_commit(self) -> PreparedCommit | None:
        """创建稳定提交快照，但不确认或清除脏状态。"""
        if (
            not self._dirty_records
            and not self._dirty_context
            and not self._dirty_plan
            and not self._dirty_calls
        ):
            return None
        self._materialize_all_view_streams()
        dirty_records = set(self._dirty_records)
        dirty_calls = set(self._dirty_calls)
        pending = self._pending_by_record()
        records: dict[str, dict[str, Any]] = {}
        record_deltas: dict[str, dict[str, Any]] = {}
        record_delta_snapshots: dict[str, dict[str, Any]] = {}
        stream_state: dict[tuple[str, str], str] = {}
        view_keys: dict[str, frozenset[str]] = {}
        for record in self.records:
            if record.id not in dirty_records:
                continue
            chunks = pending.get(record.id, {})
            if record.id in self._committed_records and self._can_append(record, chunks):
                record_deltas[record.id] = self._record_delta(record, chunks)
                record_delta_snapshots[record.id] = self._snapshot_value(
                    self._static_projection(record),
                )
            else:
                records[record.id] = self._snapshot_value(self._commit_projection(record))
            for key, after in self._stream_after(record).items():
                stream_state[(record.id, key)] = after
            view_keys[record.id] = self._view_keys(record)
        payload: dict[str, Any] = {}
        if records:
            payload["records"] = list(records.values())
        if record_deltas:
            payload["record_deltas"] = list(record_deltas.values())
        context_ids = list(self.context_ids)
        if self._dirty_context:
            payload["context"] = self._snapshot_value(self._context_delta_preview())
        plan = self._snapshot_value(self.plan) if self._dirty_plan else None
        if plan is not None:
            payload["plan"] = plan
        calls = {
            (call.call_id, call.attempt): self._snapshot_value(call.to_dict())
            for call in self.llm_calls
            if (call.call_id, call.attempt) in dirty_calls
        }
        if calls:
            payload["llm_calls"] = list(calls.values())
        return PreparedCommit(
            payload=payload,
            records=records,
            context_ids=context_ids,
            plan=plan,
            calls=calls,
            stream_deltas={key: list(value) for key, value in self._stream_pending.items()},
            record_delta_snapshots=record_delta_snapshots,
            stream_state=stream_state,
            view_keys=view_keys,
        )

    def ack_commit(self, commit: PreparedCommit) -> None:
        """确认已经成功追加的快照，保留提交期间发生的新修改。

        写侧无法按增量对位时会登记 ``commit.resync``：那段正文虽已写入，但只有
        整份快照才能让磁盘与内存收敛，因此在这里退回整份重写，调用方不必分别处理。
        """
        for key, chunks in commit.stream_deltas.items():
            pending = self._stream_pending.get(key, [])
            if pending[:len(chunks)] == chunks:
                del pending[:len(chunks)]
            if pending:
                self._stream_pending[key] = pending
            else:
                self._stream_pending.pop(key, None)
        self._committed_streams.update(commit.stream_state)
        self._committed_view_keys.update(commit.view_keys)
        for record_id, snapshot in commit.records.items():
            record = next((item for item in self.records if item.id == record_id), None)
            if record is None:
                continue
            self._committed_records.add(record_id)
            committed = self._commit_projection(record) == snapshot
            if not committed:
                committed = self._static_projection(record) == self._static_projection_from_full(snapshot)
            if committed and not self._has_pending(record_id):
                self._dirty_records.discard(record_id)
        for record_id, snapshot in commit.record_delta_snapshots.items():
            record = next((item for item in self.records if item.id == record_id), None)
            if record is None:
                self._dirty_records.discard(record_id)
                continue
            if self._static_projection(record) == snapshot and not self._has_pending(record_id):
                self._dirty_records.discard(record_id)
        if self.context_ids == commit.context_ids:
            self._committed_context_ids = list(commit.context_ids)
            self._dirty_context = False
        else:
            self._committed_context_ids = list(commit.context_ids)
            self._dirty_context = True
        if commit.plan is not None and self.plan == commit.plan:
            self._dirty_plan = False
        for key, snapshot in commit.calls.items():
            call = next(
                (item for item in self.llm_calls if (item.call_id, item.attempt) == key),
                None,
            )
            if call is not None and call.to_dict() == snapshot:
                self._dirty_calls.discard(key)
        if commit.resync:
            self.invalidate_append_baseline(commit.resync)
        self._last_checkpoint_at = time.monotonic()

    def requeue_commit(self, _commit: PreparedCommit) -> None:
        """保留失败提交的 dirty 状态；下一次检查点会重试。"""

    def checkpoint_due(self, *, force: bool = False, now: float | None = None) -> bool:
        """返回当前状态是否应执行一次检查点。

        间隔由流式正文长度决定（复用前台刷新的档位）：正文越长落盘越
        稀疏，没有流式内容时用最短间隔，保证小改动也能及时落盘。
        """
        if not (
            self._dirty_records or self._dirty_context or self._dirty_plan
            or self._dirty_calls or self._stream_pending
        ):
            return False
        if force:
            return True
        current = time.monotonic() if now is None else now
        interval = self.stream_policy.interval(self._stream_length())
        return current - self._last_checkpoint_at >= interval

    def take_commit(self) -> dict[str, Any] | None:
        """返回并确认一份本地测试/同步调用使用的提交载荷。

        生产写盘路径必须使用 ``prepare_commit``、成功写入后再调用 ``ack_commit``。
        """
        commit = self.prepare_commit()
        if commit is None:
            return None
        self.ack_commit(commit)
        return commit.payload

    def _context_delta_preview(self) -> dict[str, Any]:
        """返回上下文投影相对上次提交的增量。

        日志是追加式的：整段重发一份 ID 列表会让同一批 ID 反复落盘，且随上下文
        增长呈平方级写入。这里只提交「保留的公共前缀 + 新增尾部」，截断与整体替换
        同样用这一形式表达。

        Returns:
            ``{"keep": 公共前缀长度, "append": 新增的尾部记录 ID}``。
        """
        committed = self._committed_context_ids
        limit = min(len(committed), len(self.context_ids))
        keep = 0
        while keep < limit and committed[keep] == self.context_ids[keep]:
            keep += 1
        return {"keep": keep, "append": list(self.context_ids[keep:])}

    @staticmethod
    def _snapshot_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: SessionState._snapshot_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [SessionState._snapshot_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(SessionState._snapshot_value(item) for item in value)
        return value

    @staticmethod
    def _commit_projection(record: SessionRecord) -> dict[str, Any]:
        """返回一条记录的提交投影。

        Args:
            record: 待提交的记录。

        Returns:
            可直接编码的记录字典；视图数据是浅拷贝，模型消息仍是原对象。
        """
        projection = record.to_dict()
        view = projection.get("view")
        if isinstance(view, dict) and isinstance(view.get("data"), dict):
            view["data"] = dict(view["data"])
        return projection

    def update_plan(self, plan: dict[str, Any]) -> None:
        """替换会话计划快照并标记待提交。

        Args:
            plan: 新的计划数据；传入空 dict 表示清空计划。
        """
        self.plan = copy.deepcopy(plan)
        self._dirty_plan = True

    def tool_result_text(self, tool_call_id: str | None) -> str | None:
        """返回指定工具调用唯一一份结果正文。

        Args:
            tool_call_id: 工具调用 ID。

        Returns:
            对应 tool 消息的正文；未找到时返回 None。
        """
        if not tool_call_id:
            return None
        for record in self.records:
            message = record.model_message
            if (
                isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_call_id") == tool_call_id
            ):
                content = message.get("content")
                return content if isinstance(content, str) else None
        return None

    def context_messages(self) -> list[dict[str, Any]]:
        """返回模型上下文投影，元素与记录共享同一批 dict 对象。"""
        by_id = {record.id: record for record in self.records}
        return [
            by_id[record_id].model_message
            for record_id in self.context_ids
            if record_id in by_id and by_id[record_id].model_message is not None
        ]

    def visible_records(self) -> list[SessionRecord]:
        self._materialize_all_view_streams()
        return [
            record
            for record in self.records
            if record.view is not None and record.view.kind != "subagent"
        ]

    def settle_view_text(self, redact: Callable[[str], object] | None = None) -> set[str]:
        """按整段文本复核流式正文的脱敏结果，必要时整份重写该记录。

        流式正文按块脱敏，看不到跨块的敏感模式与 URL 改写；回合结束后按整段
        复核一次，让显示、内存与磁盘收敛到同一份脱敏文本。改写的记录会退回整份
        快照（正文已被整段结果替换，不能再按增量对位）。

        Args:
            redact: 脱敏函数；None 时只物化正文缓冲，不做改写。

        Returns:
            正文被整段复核改写的记录 ID 集合。
        """
        self._materialize_all_view_streams()
        settled: set[str] = set()
        for record_id in sorted(self._streamed_records):
            record = self._find(record_id=record_id)
            if record is None or record.view is None:
                continue
            changed = False
            for key in _STREAM_KEYS:
                value = record.view.data.get(key)
                if not isinstance(value, str) or not value:
                    continue
                safe = redact(value) if redact is not None else value
                if isinstance(safe, str) and safe != value:
                    record.view.data[key] = safe
                    changed = True
            if changed:
                self.invalidate_append_baseline([record.id])
                settled.add(record.id)
        self._streamed_records.clear()
        return settled

    def invalidate_append_baseline(self, record_ids: Iterable[str]) -> None:
        """让指定记录的下一次提交退回整份快照。

        用于正文被整体替换（整段脱敏、外部重写）后无法再按增量对位的情况；
        快照会把完整正文重新写入日志，同时刷新写侧的内容摘要基线。

        Args:
            record_ids: 需要整份重写的记录 ID。

        Returns:
            None。
        """
        for record_id in record_ids:
            if not isinstance(record_id, str) or not record_id:
                continue
            self._committed_records.discard(record_id)
            self._committed_view_keys.pop(record_id, None)
            for key in _STREAM_KEYS:
                self._committed_streams.pop((record_id, key), None)
                self._stream_pending.pop((record_id, key), None)
            self._clear_record_view_streams(record_id)
            self._dirty_records.add(record_id)

    def subagent_views(self) -> list[dict[str, Any]]:
        """Return persisted read-only projections for the session's subagents."""
        self._materialize_all_view_streams()
        return [
            record.view.data
            for record in self.records
            if record.view is not None and record.view.kind == "subagent"
        ]

    def input_history(self) -> list[str]:
        return [
            record.raw_input
            for record in self.records
            if record.recallable and record.raw_input is not None
        ]

    def append_user(self, raw_input: str, model_message: dict[str, Any] | None = None) -> str:
        """新增用户记录；hook/reminder 注入后的消息稍后可绑定到同一记录。"""
        entry = raw_input.strip("\n")
        recallable = bool(entry.strip())
        if recallable and self.input_history()[-1:] == [entry]:
            recallable = False
        return self.append_record(
            kind="user",
            model_message=model_message,
            view=ViewPayload("user", {"text": raw_input}),
            raw_input=entry if entry else raw_input,
            recallable=recallable,
        )

    def append_context(
        self,
        message: dict[str, Any],
        *,
        kind: str = "internal",
        correlation_id: str | None = None,
    ) -> str:
        return self.append_record(
            kind=kind,
            model_message=message,
            correlation_id=correlation_id,
        )

    def append_record(
        self,
        *,
        kind: str,
        model_message: dict[str, Any] | None = None,
        view: ViewPayload | None = None,
        raw_input: str | None = None,
        recallable: bool = False,
        correlation_id: str | None = None,
        timestamp: float | None = None,
    ) -> str:
        record_id = uuid.uuid4().hex
        record = SessionRecord(
            id=record_id,
            timestamp=time.time() if timestamp is None else timestamp,
            kind=kind,
            model_message=model_message,
            view=view,
            raw_input=raw_input,
            recallable=recallable,
            correlation_id=correlation_id,
        )
        self.records.append(record)
        self._dirty_records.add(record_id)
        if model_message is not None:
            self.context_ids.append(record_id)
            self._dirty_context = True
        return record_id

    def bind_model_message(
        self,
        message: dict[str, Any],
        *,
        record_id: str | None = None,
        correlation_id: str | None = None,
        kind: str = "internal",
    ) -> str:
        record = (
            self._find(record_id=record_id, correlation_id=correlation_id, kind=kind)
            if record_id is not None or correlation_id is not None
            else None
        )
        if record is None:
            return self.append_context(message, kind=kind, correlation_id=correlation_id)
        self._materialize_record_view(record)
        record.model_message = message
        self._adopt_equal_text(record)
        self._dirty_records.add(record.id)
        if record.id not in self.context_ids:
            self.context_ids.append(record.id)
            self._dirty_context = True
        return record.id

    @staticmethod
    def _materialized(message: dict[str, Any]) -> dict[str, Any]:
        """返回消息的具体化视图；载体消息按需重建派生字段。

        Args:
            message: 模型消息。

        Returns:
            可直接读取 ``content``/``tool_calls`` 的消息；无载体时原样返回。
        """
        try:
            from src.llm.message_view import materialize_assistant
        except ImportError:  # pragma: no cover - 缺依赖时退化为原消息
            return message
        view = materialize_assistant(message)
        return view if isinstance(view, dict) else message

    def _adopt_equal_text(self, record: SessionRecord) -> None:
        """让视图与输入历史复用模型消息里已有的字符串对象。

        同一段正文同时服务模型上下文与前台展示时，只有逐字符相等才替换对象引用，
        因此内存里只有一份正文；内容不同（例如重试残片、hook 改写过的输入）保持
        原样，避免把展示语义合并掉。

        Args:
            record: 刚绑定模型消息的记录。

        Returns:
            None。
        """
        message = record.model_message
        if not isinstance(message, dict):
            return
        view = self._materialized(message)
        candidates: list[str] = []
        for key in ("content", "reasoning_content", "reasoning"):
            value = view.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        content = view.get("content")
        if record.view is not None and candidates:
            for key, value in list(record.view.data.items()):
                if not isinstance(value, str) or not value:
                    continue
                for text in candidates:
                    if value is not text and value == text:
                        record.view.data[key] = text
                        break
        if isinstance(content, str) and content and record.raw_input == content:
            record.raw_input = content

    def tool_call_arguments(self, tool_call_id: str | None) -> dict[str, Any] | None:
        """返回指定工具调用的参数，取自已落库的唯一一份工具调用记录。

        Args:
            tool_call_id: 工具调用 ID。

        Returns:
            参数 dict；未找到、参数不是 JSON 字符串或不是对象时返回 None。
        """
        if not tool_call_id:
            return None
        for record in self.records:
            message = record.model_message
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = self._materialized(message).get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or call.get("id") != tool_call_id:
                    continue
                function = call.get("function")
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if isinstance(arguments, dict):
                    return arguments
                if not isinstance(arguments, str):
                    return None
                try:
                    parsed = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    return None
                return parsed if isinstance(parsed, dict) else None
        return None

    def replace_context(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        preserve_positions: bool = False,
    ) -> None:
        """替换上下文投影，保留所有已有可见记录。"""
        messages = list(messages)
        if preserve_positions and len(messages) == len(self.context_ids):
            for index, message in enumerate(messages):
                self.set_context_message(index, message)
            return
        old_ids = set(self.context_ids)
        remaining = list(self.context_ids)
        by_id = {record.id: record for record in self.records}
        new_ids: list[str] = []
        for message in messages:
            match_id = next(
                (
                    record_id
                    for record_id in remaining
                    if by_id[record_id].model_message == message
                ),
                None,
            )
            if match_id is not None:
                remaining.remove(match_id)
                new_ids.append(match_id)
                continue
            new_ids.append(self.append_record(kind="internal", model_message=message))
        self.context_ids = new_ids
        self._dirty_context = True
        self._release_model_messages(old_ids - set(new_ids))

    def truncate_context(self, length: int) -> None:
        retained = self.context_ids[:max(0, length)]
        self._release_model_messages(set(self.context_ids) - set(retained))
        self.context_ids = retained
        self._dirty_context = True

    def set_context_message(self, index: int, message: dict[str, Any]) -> None:
        """替换指定位置的上下文消息。

        provider 归一化会返回一份新消息对象；内容未变时只交换对象引用、不标记
        待提交，否则每轮 LLM 调用都会把整段上下文重写成新的一批日志。

        Args:
            index: 上下文下标。
            message: 新的模型消息。
        """
        record = self._record_by_id(self.context_ids[index])
        if record.model_message is message:
            return
        if record.model_message != message:
            self._dirty_records.add(record.id)
        record.model_message = message

    def record_view(
        self,
        view: ViewPayload,
        *,
        kind: str,
        correlation_id: str | None = None,
        timestamp: float | None = None,
        merge: bool = False,
    ) -> str:
        record = self._find(correlation_id=correlation_id, kind=kind) if correlation_id else None
        if record is None:
            stream_values: dict[str, str] = {}
            if merge:
                for key in ("content", "thinking"):
                    value = view.data.get(key)
                    if isinstance(value, str):
                        stream_values[key] = value
                        view.data[key] = ""
            record_id = self.append_record(
                kind=kind,
                view=view,
                correlation_id=correlation_id,
                timestamp=timestamp,
            )
            for key, value in stream_values.items():
                self._append_view_stream(record_id, key, value)
            return record_id
        self._dirty_records.add(record.id)
        if not merge or record.view is None or record.view.kind != view.kind:
            self._clear_record_view_streams(record.id)
            record.view = view
            return record.id
        for key, value in view.data.items():
            if key in {"content", "thinking"} and isinstance(value, str):
                self._append_view_stream(record.id, key, value)
            else:
                record.view.data[key] = value
        return record.id

    def record_subagent_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        """Persist one complete subagent read-model snapshot.

        Subagent projections are deliberately hidden from the foreground transcript;
        they are restored into ``AgentViewStore`` when the session is resumed.
        """
        agent_uuid = snapshot.get("uuid")
        if not isinstance(agent_uuid, str) or not agent_uuid:
            return None
        return self.record_view(
            ViewPayload("subagent", snapshot),
            kind="subagent",
            correlation_id=agent_uuid,
        )

    def record_llm_event(self, event: object) -> None:
        """把 LLM 生命周期事件归并成 attempt 级核账记录。"""
        from src.events.types import LLMCallCompleted, LLMCallFailed, LLMCallStarted

        if not isinstance(event, (LLMCallStarted, LLMCallCompleted, LLMCallFailed)):
            return
        call_id = event.call_id
        if not call_id:
            return
        attempt = event.attempt if isinstance(event, (LLMCallStarted, LLMCallCompleted)) else max(1, event.attempts)
        record = next(
            (item for item in self.llm_calls if item.call_id == call_id and item.attempt == attempt),
            None,
        )
        if record is None:
            record = LLMCallRecord(
                call_id=call_id,
                attempt=attempt,
                model=getattr(event, "model", "") or event.source,
                caller_uuid=event.caller_uuid,
                caller_type=event.caller_agent_type,
                reasoning_effort=getattr(event, "reasoning_effort", None),
                phase=getattr(event, "phase", "execute"),
                started_at=event.timestamp,
            )
            self.llm_calls.append(record)
        if isinstance(event, LLMCallStarted):
            record.model = event.model
            record.caller_uuid = event.caller_uuid
            record.caller_type = event.caller_agent_type
            record.reasoning_effort = event.reasoning_effort
            record.phase = event.phase
            record.started_at = event.timestamp
            self._dirty_calls.add((record.call_id, record.attempt))
            return
        record.completed_at = event.timestamp
        if isinstance(event, LLMCallFailed):
            record.outcome = event.error_kind or "failed"
            self._dirty_calls.add((record.call_id, record.attempt))
            return
        record.outcome = event.outcome
        for key in (
            "input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
        ):
            setattr(record, key, getattr(event, key))
        self._dirty_calls.add((record.call_id, record.attempt))

    def record_event(self, event: object) -> str | None:
        """把一个已通过前后台筛选的可见事件归并到展示投影。"""
        from rich.text import Text

        from src.events.menu import MenuRequest, PermissionMenu
        from src.events.types import (
            CompactDelta,
            InteractionCompleted,
            LLMCallFailed,
            LLMLengthRetrying,
            LLMRetrying,
            OutputRequested,
            PermissionNotice,
            ResponseDelta,
            ThinkingDelta,
            ToolCallCompleted,
            ToolCallStarted,
        )

        timestamp = getattr(event, "timestamp", time.time())
        if isinstance(event, PermissionMenu):
            content = f"工具请求权限\n工具: {event.tool_name}\n内容: {event.detail}"
            if event.reason:
                content += f"\n原因: {event.reason}"
            return self.record_view(
                ViewPayload("output", {"content": content, "markdown": False}),
                kind="interaction_prompt",
                timestamp=timestamp,
            )
        if isinstance(event, MenuRequest):
            prompt = str(getattr(event, "prompt", "")).strip()
            if prompt:
                return self.record_view(
                    ViewPayload(
                        "output",
                        {"content": prompt, "markdown": bool(getattr(event, "markdown", False))},
                    ),
                    kind="interaction_prompt",
                    timestamp=timestamp,
                )
            return None
        if isinstance(event, (ResponseDelta, ThinkingDelta)):
            key = "content" if isinstance(event, ResponseDelta) else "thinking"
            return self.record_view(
                ViewPayload("assistant", {key: event.content}),
                kind="assistant",
                correlation_id=event.call_id or None,
                timestamp=timestamp,
                merge=True,
            )
        if isinstance(event, (LLMRetrying, LLMLengthRetrying, LLMCallFailed)):
            data = self._event_data(event)
            data["event_type"] = event.type
            return self.record_view(
                ViewPayload("assistant", data),
                kind="assistant",
                correlation_id=event.call_id or None,
                timestamp=timestamp,
                merge=True,
            )
        if isinstance(event, (ToolCallStarted, ToolCallCompleted)):
            phase = "started" if isinstance(event, ToolCallStarted) else "completed"
            data = self._event_data(event)
            _strip_rendered_content(data.get("display"))
            _strip_derived_preview(data)
            return self.record_view(
                ViewPayload("tool", {phase: data}),
                kind="tool",
                correlation_id=event.tool_call_id or None,
                timestamp=timestamp,
                merge=True,
            )
        if isinstance(event, OutputRequested):
            content = event.content.plain if isinstance(event.content, Text) else str(event.content)
            return self.record_view(
                ViewPayload("output", {"content": content, "markdown": event.markdown}),
                kind="output",
                timestamp=timestamp,
            )
        if isinstance(event, InteractionCompleted):
            return self.record_view(
                ViewPayload("output", {
                    "content": event.summary,
                    "markdown": False,
                    "request_type": event.request_type,
                    "cancelled": event.cancelled,
                    "request": copy.deepcopy(event.request),
                    "answer": event.answer,
                }),
                kind="interaction",
                timestamp=timestamp,
            )
        if isinstance(event, (CompactDelta, PermissionNotice)):
            data = self._event_data(event)
            data["event_type"] = event.type
            return self.record_view(
                ViewPayload("event", data),
                kind="view",
                timestamp=timestamp,
            )
        return None

    @staticmethod
    def _event_data(event: object) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in vars(event).items():
            if key in {"future", "level", "type"}:
                continue
            if is_dataclass(value):
                data[key] = asdict(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                data[key] = value
            elif isinstance(value, (list, dict)):
                data[key] = copy.deepcopy(value)
            else:
                data[key] = str(value)
        return data

    def _find(
        self,
        *,
        record_id: str | None = None,
        correlation_id: str | None = None,
        kind: str | None = None,
    ) -> SessionRecord | None:
        for record in reversed(self.records):
            if record_id is not None and record.id != record_id:
                continue
            if correlation_id is not None and record.correlation_id != correlation_id:
                continue
            if kind is not None and record.kind != kind:
                continue
            return record
        return None

    def _record_by_id(self, record_id: str) -> SessionRecord:
        record = self._find(record_id=record_id)
        if record is None:
            raise KeyError(record_id)
        return record

    def _append_view_stream(self, record_id: str, key: str, value: str) -> None:
        stream_key = (record_id, key)
        self._dirty_records.add(record_id)
        self._streamed_records.add(record_id)
        buffer = self._view_streams.get(stream_key)
        if buffer is None:
            record = self._record_by_id(record_id)
            existing = ""
            if record.view is not None:
                current = record.view.data.get(key, "")
                existing = current if isinstance(current, str) else str(current)
                record.view.data[key] = ""
            buffer = _TextChunks()
            buffer.append(existing)
            self._view_streams[stream_key] = buffer
        buffer.append(value)
        self._stream_pending.setdefault(stream_key, []).append(value)

    def _pending_by_record(self) -> dict[str, dict[str, list[str]]]:
        """按记录归并尚未确认的流式正文片段。"""
        result: dict[str, dict[str, list[str]]] = {}
        for (record_id, key), chunks in self._stream_pending.items():
            if chunks:
                result.setdefault(record_id, {})[key] = chunks
        return result

    def _has_pending(self, record_id: str) -> bool:
        """返回该记录是否还有未确认的流式正文。"""
        return any(
            chunks for (item_id, _key), chunks in self._stream_pending.items()
            if item_id == record_id
        )

    def _stream_length(self) -> int:
        """返回最长的流式正文长度，用于检查点档位判定。

        口径与前台渲染共用（整段正文而非未确认尾部）：缓冲区尚未物化时直接读
        缓冲区，已被 `prepare_commit` 或可见记录投影物化时回退到视图正文。
        """
        longest = 0
        for (record_id, key), chunks in self._stream_pending.items():
            if not chunks:
                continue
            buffer = self._view_streams.get((record_id, key))
            if buffer is not None:
                longest = max(longest, buffer.length)
                continue
            record = self._find(record_id=record_id)
            value = (
                record.view.data.get(key)
                if record is not None and record.view is not None else None
            )
            longest = max(longest, len(value) if isinstance(value, str) else 0)
        return longest

    @staticmethod
    def _view_keys(record: SessionRecord) -> frozenset[str]:
        """返回记录视图的数据键集合；视图缺失时为空集。"""
        return frozenset(record.view.data) if record.view is not None else frozenset()

    @staticmethod
    def _stream_after(record: SessionRecord) -> dict[str, str]:
        """返回记录正文的写侧摘要，用于推进已落盘基线。"""
        data = record.view.data if record.view is not None else {}
        result: dict[str, str] = {}
        for key in _STREAM_KEYS:
            value = data.get(key)
            result[key] = text_digest(value if isinstance(value, str) else "")
        return result

    def _remember_committed(self, record: SessionRecord) -> None:
        """登记一条已经完整落盘的记录：正文摘要与视图键集合。"""
        for key, after in self._stream_after(record).items():
            self._committed_streams[(record.id, key)] = after
        self._committed_view_keys[record.id] = self._view_keys(record)

    def _can_append(self, record: SessionRecord, pending: dict[str, list[str]]) -> bool:
        """判断记录能否只用「静态投影 + 正文追加」表达。

        记录还没有视图、视图键被删除（``set`` 只能覆盖与新增，无法表达删除），或
        正文与已落盘基线对不上（正文被替换、被截断）时返回 False，调用方必须回退
        整份快照，否则磁盘会留下错位或过期的正文。

        Args:
            record: 待提交的记录。
            pending: 该记录尚未确认的流式正文片段，键为 ``content``/``thinking``。

        Returns:
            可以只提交增量时为 True。
        """
        if record.view is None:
            return False
        if self._committed_view_keys.get(record.id, frozenset()) - self._view_keys(record):
            return False
        for key in _STREAM_KEYS:
            value = record.view.data.get(key)
            text = value if isinstance(value, str) else ""
            tail = "".join(pending.get(key, ()))
            if tail and not text.endswith(tail):
                return False
            base = text[: len(text) - len(tail)] if tail else text
            if text_digest(base) != self._committed_streams.get((record.id, key), _EMPTY_DIGEST):
                return False
        return True

    def _record_delta(
        self, record: SessionRecord, pending: dict[str, list[str]],
    ) -> dict[str, Any]:
        """构造一条记录增量：静态投影 + 本次待追加的流式正文。

        静态投影按键整体覆盖（末次写胜），因此 ``model_message``、``raw_input``
        等字段不需要单独追踪改动；正文只提交本次未确认的尾部，读取端按 ``seq``
        顺序追加还原。
        """
        static = self._static_projection(record)
        data = record.view.data if record.view is not None else {}
        appended: dict[str, str] = {}
        boundaries: dict[str, dict[str, str]] = {}
        for key in _STREAM_KEYS:
            chunks = pending.get(key)
            if not chunks:
                continue
            value = data.get(key)
            text = value if isinstance(value, str) else ""
            tail = "".join(chunks)
            appended[key] = tail
            boundaries[key] = {
                "base": text_digest(text[: len(text) - len(tail)]),
                "after": text_digest(text),
            }
        return {
            "id": record.id,
            "fields": static["fields"],
            "view": {
                "kind": static["view"]["kind"],
                "set": static["view"]["set"],
                "append": appended,
            },
            "stream_boundaries": boundaries,
        }

    @staticmethod
    def _static_projection(record: SessionRecord) -> dict[str, Any]:
        """返回记录的静态投影（字段 + 非正文视图键），增量与摘要比较共用此形状。"""
        view: dict[str, Any] = {"kind": "", "set": {}}
        if record.view is not None:
            view["kind"] = record.view.kind
            view["set"] = {
                key: value for key, value in record.view.data.items()
                if key not in {"content", "thinking"}
            }
        return {
            "id": record.id,
            "fields": {
                "timestamp": record.timestamp,
                "kind": record.kind,
                "model_message": record.model_message,
                "raw_input": record.raw_input,
                "recallable": record.recallable,
                "correlation_id": record.correlation_id,
            },
            "view": view,
        }

    @staticmethod
    def _static_projection_from_full(value: dict[str, Any]) -> dict[str, Any]:
        """把整份记录快照归一成与 `_static_projection` 相同的形状。"""
        view_value = value.get("view")
        view_data = view_value.get("data") if isinstance(view_value, dict) else None
        return {
            "id": value.get("id"),
            "fields": {
                "timestamp": value.get("timestamp"),
                "kind": value.get("kind"),
                "model_message": value.get("model_message"),
                "raw_input": value.get("raw_input"),
                "recallable": value.get("recallable"),
                "correlation_id": value.get("correlation_id"),
            },
            "view": {
                "kind": view_value.get("kind") if isinstance(view_value, dict) else "",
                "set": {
                    key: item for key, item in (view_data.items() if isinstance(view_data, dict) else [])
                    if key not in {"content", "thinking"}
                },
            },
        }

    def _materialize_record_view(self, record: SessionRecord) -> None:
        if record.view is None:
            self._clear_record_view_streams(record.id)
            return
        for key in ("content", "thinking"):
            buffer = self._view_streams.pop((record.id, key), None)
            if buffer is not None:
                record.view.data[key] = buffer.materialize()
                self._dirty_records.add(record.id)

    def _materialize_all_view_streams(self) -> None:
        if not self._view_streams:
            return
        by_id = {record.id: record for record in self.records}
        for record_id, _key in list(self._view_streams):
            record = by_id.get(record_id)
            if record is not None:
                self._materialize_record_view(record)
            else:
                self._clear_record_view_streams(record_id)

    def _clear_record_view_streams(self, record_id: str) -> None:
        self._view_streams.pop((record_id, "content"), None)
        self._view_streams.pop((record_id, "thinking"), None)

    def _release_model_messages(self, record_ids: set[str]) -> None:
        if not record_ids:
            return
        for record in self.records:
            if record.id in record_ids and record.model_message is not None:
                record.model_message = None
                self._dirty_records.add(record.id)
        self._dirty_context = True
