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
    LLMLengthRetrying,
    LLMRetrying,
    OutputRequested,
    PermissionNotice,
    ResponseDelta,
    SubagentLifecycle,
    PlanStateChanged,
    TaskStateChanged,
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
    ModelMenu,
    MenuRequest,
    PermissionMenu,
    TranscriptView,
    UiRequest,
    ViewRequest,
)

# 全部事件的联合类型（含菜单事件）。原置于 types.py，因 types.py 不再依赖 menu.py
# 而上移至包出口，避免 types ↔ menu 循环导入。
AgentEvent = Union[
    InputMenu, OutputRequested, InterruptRequested,
    PermissionNotice, PermissionMenu, ChoiceMenu, ChoiceInputMenu, FormMenu, ModelMenu,
    TranscriptView,
    CompactDelta, ToolCallCompleted, ToolCallStarted,
    LLMCallCompleted, LLMCallStarted, LLMRetrying, LLMLengthRetrying, LLMCallFailed,
    ResponseDelta, ThinkingDelta,
    AgentStateChanged, SubagentLifecycle, PlanStateChanged, TaskStateChanged,
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
    "LLMLengthRetrying",
    "LLMCallFailed",
    # 菜单/交互事件
    "UiRequest",
    "MenuRequest",
    "ViewRequest",
    "PermissionMenu",
    "InputMenu",
    "ChoiceMenu",
    "ModelMenu",
    "ChoiceInputMenu",
    "FormMenu",
    "FormQuestion",
    "TranscriptView",
    "PlanStateChanged",
    "TaskStateChanged",
]
