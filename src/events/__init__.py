"""events 包出口 — 聚合 levels/bus/types/menu 的公开符号，并定义 AgentEvent 联合。"""

from typing import Union

from src.events.levels import EventLevel
from src.events.bus import EventBus, NoEventSubscribers, emit_telemetry_safely
from src.events.types import (
    AgentStateChanged,
    CompactDelta,
    Event,
    InterruptRequested,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMRetrying,
    OutputRequested,
    PermissionNotice,
    ResponseDelta,
    SubagentLifecycle,
    PermissionModeChanged,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    MenuRequest,
    PermissionMenu,
    TranscriptView,
)

# 全部事件的联合类型（含菜单事件）。原置于 types.py，因 types.py 不再依赖 menu.py
# 而上移至包出口，避免 types ↔ menu 循环导入。
AgentEvent = Union[
    InputMenu, OutputRequested, InterruptRequested,
    PermissionNotice, PermissionMenu, ChoiceMenu, ChoiceInputMenu, FormMenu,
    CompactDelta, ToolCallCompleted, ToolCallStarted,
    LLMCallCompleted, LLMCallStarted, LLMRetrying, LLMCallFailed,
    ResponseDelta, ThinkingDelta,
    AgentStateChanged, SubagentLifecycle, PermissionModeChanged,
]

__all__ = [
    "EventLevel",
    "EventBus",
    "NoEventSubscribers",
    "emit_telemetry_safely",
    "Event",
    "AgentEvent",
    "CompactDelta",
    "InterruptRequested",
    "OutputRequested",
    "PermissionNotice",
    "ToolCallCompleted",
    "ToolCallStarted",
    "LLMCallCompleted",
    "LLMCallStarted",
    "LLMRetrying",
    "LLMCallFailed",
    # 菜单/交互事件
    "MenuRequest",
    "PermissionMenu",
    "InputMenu",
    "ChoiceMenu",
    "ChoiceInputMenu",
    "FormMenu",
    "FormQuestion",
    "TranscriptView",
    "PermissionModeChanged",
]
