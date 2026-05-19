"""事件类型定义 — 所有 EventBus 可发布的事件。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal, Union

from src.events.levels import EventLevel


@dataclass
class Event:
    """事件基类。"""

    timestamp: float
    source: str
    level: EventLevel
    type: str = ""


class InputInterrupted(Exception):
    """用户取消了一个输入请求。"""


@dataclass
class UserInputRequest(Event):
    """需要 UI 通过 future 返回用户输入的事件基类。"""

    future: asyncio.Future[str] | None = None

    def complete(self, value: str) -> None:
        if self.future is not None and not self.future.done():
            self.future.set_result(value)

    def interrupt(self) -> None:
        if self.future is not None and not self.future.done():
            self.future.set_exception(InputInterrupted())

    def fail(self, exc: BaseException) -> None:
        if self.future is not None and not self.future.done():
            self.future.set_exception(exc)


# --- PROGRESS 级别 ---

@dataclass
class ResponseDelta(Event):
    """流式回应 — 默认可见。"""
    content: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["token_delta"] = field(default="token_delta", init=False)


@dataclass
class ThinkingDelta(Event):
    """思考过程 — 默认可见。"""
    content: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["thinking_delta"] = field(default="thinking_delta", init=False)

@dataclass
class ErrorOccurred(Event):
    error: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["error"] = field(default="error", init=False)

@dataclass
class CompactDelta(Event):
    content: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["compact_delta"] = field(default="compact_delta", init=False)


@dataclass
class LLMCallStarted(Event):
    """LLM 调用开始时的 token 估算信息。"""
    model: str = ""
    estimated_input_tokens: int = 0
    message_count: int = 0
    tool_count: int = 0
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_started"] = field(default="llm_call_started", init=False)


@dataclass
class LLMCallCompleted(Event):
    """LLM 调用完成后的 token usage 与速度信息。"""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    duration_seconds: float | None = None
    output_tokens_per_second: float | None = None
    total_tokens_per_second: float | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_completed"] = field(default="llm_call_completed", init=False)


@dataclass
class OutputRequested(Event):
    """请求 UI 串行输出文本。"""
    content: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["output_requested"] = field(default="output_requested", init=False)


@dataclass
class PermissionNotice(Event):
    """工具权限状态通知，供 UI 自行组织展示。"""
    status: Literal["allow", "deny"] = "allow"
    tool_name: str = ""
    detail: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_notice"] = field(default="permission_notice", init=False)


@dataclass
class PermissionRequested(UserInputRequest):
    """请求 UI 读取工具权限确认，并通过 future 返回结果。"""
    tool_name: str = ""
    detail: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_requested"] = field(default="permission_requested", init=False)


@dataclass
class InputRequested(UserInputRequest):
    """请求 UI 串行读取用户输入，并通过 future 返回结果。"""
    prompt: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["input_requested"] = field(default="input_requested", init=False)


# 联合类型
AgentEvent = Union[
    ErrorOccurred, InputRequested, OutputRequested,
    PermissionNotice, PermissionRequested,
    LLMCallCompleted, LLMCallStarted,
    ResponseDelta, ThinkingDelta,
]
