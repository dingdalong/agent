"""单一会话状态及其上下文、可见历史和输入回溯投影。"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable


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
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "model_message": copy.deepcopy(self.model_message),
            "view": asdict(self.view) if self.view is not None else None,
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
class SessionState:
    """会话状态权威写入点。"""

    records: list[SessionRecord] = field(default_factory=list)
    context_ids: list[str] = field(default_factory=list)
    _view_streams: dict[tuple[str, str], _TextChunks] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @classmethod
    def from_dict(cls, value: object) -> SessionState | None:
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != 1
        ):
            return None
        raw_records = value.get("records")
        context_ids = value.get("context_ids")
        if not isinstance(raw_records, list) or not isinstance(context_ids, list):
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
        return cls(records=records, context_ids=list(context_ids))

    def to_dict(self) -> dict[str, Any]:
        self._materialize_all_view_streams()
        return {
            "version": 1,
            "records": [record.to_dict() for record in self.records],
            "context_ids": list(self.context_ids),
        }

    def context_messages(self) -> list[dict[str, Any]]:
        by_id = {record.id: record for record in self.records}
        return [
            copy.deepcopy(by_id[record_id].model_message)
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

    def subagent_views(self) -> list[dict[str, Any]]:
        """Return persisted read-only projections for the session's subagents."""
        self._materialize_all_view_streams()
        return [
            copy.deepcopy(record.view.data)
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
            model_message=copy.deepcopy(model_message),
            view=copy.deepcopy(view),
            raw_input=raw_input,
            recallable=recallable,
            correlation_id=correlation_id,
        )
        self.records.append(record)
        if model_message is not None:
            self.context_ids.append(record_id)
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
        record.model_message = copy.deepcopy(message)
        if record.id not in self.context_ids:
            self.context_ids.append(record.id)
        return record.id

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
            safe_message = copy.deepcopy(message)
            match_id = next(
                (
                    record_id
                    for record_id in remaining
                    if by_id[record_id].model_message == safe_message
                ),
                None,
            )
            if match_id is not None:
                remaining.remove(match_id)
                new_ids.append(match_id)
                continue
            new_ids.append(self.append_record(kind="internal", model_message=safe_message))
        self.context_ids = new_ids
        self._release_model_messages(old_ids - set(new_ids))

    def truncate_context(self, length: int) -> None:
        retained = self.context_ids[:max(0, length)]
        self._release_model_messages(set(self.context_ids) - set(retained))
        self.context_ids = retained

    def set_context_message(self, index: int, message: dict[str, Any]) -> None:
        record = self._record_by_id(self.context_ids[index])
        record.model_message = copy.deepcopy(message)

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
            stored_view = copy.deepcopy(view)
            stream_values: dict[str, str] = {}
            if merge:
                for key in ("content", "thinking"):
                    value = stored_view.data.get(key)
                    if isinstance(value, str):
                        stream_values[key] = value
                        stored_view.data[key] = ""
            record_id = self.append_record(
                kind=kind,
                view=stored_view,
                correlation_id=correlation_id,
                timestamp=timestamp,
            )
            for key, value in stream_values.items():
                self._append_view_stream(record_id, key, value)
            return record_id
        if not merge or record.view is None or record.view.kind != view.kind:
            self._clear_record_view_streams(record.id)
            record.view = copy.deepcopy(view)
            return record.id
        for key, value in view.data.items():
            if key in {"content", "thinking"} and isinstance(value, str):
                self._append_view_stream(record.id, key, value)
            else:
                record.view.data[key] = copy.deepcopy(value)
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
            ViewPayload("subagent", copy.deepcopy(snapshot)),
            kind="subagent",
            correlation_id=agent_uuid,
        )

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
            return self.record_view(
                ViewPayload("tool", {phase: self._event_data(event)}),
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
                ViewPayload("output", {"content": event.summary, "markdown": False}),
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

    def _materialize_record_view(self, record: SessionRecord) -> None:
        if record.view is None:
            self._clear_record_view_streams(record.id)
            return
        for key in ("content", "thinking"):
            buffer = self._view_streams.pop((record.id, key), None)
            if buffer is not None:
                record.view.data[key] = buffer.materialize()

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
            if record.id in record_ids:
                record.model_message = None
