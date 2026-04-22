"""事件类型定义 — 所有 EventBus 可发布的事件。"""

from __future__ import annotations

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


# --- PROGRESS 级别 ---

@dataclass
class ResponseDelta(Event):
    """流式回应 — 默认可见。"""
    content: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["token_delta"] = field(default="token_delta", init=False)


@dataclass
class ThinkingDelta(Event):
    """思考过程 — 默认可见。"""
    content: str = ""
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

# 联合类型
AgentEvent = Union[
    ErrorOccurred,
    ResponseDelta, ThinkingDelta,
]
