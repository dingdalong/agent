"""RunContext — 贯穿图执行的泛型共享上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict


StateT = TypeVar("StateT", bound=BaseModel)
DepsT = TypeVar("DepsT", bound=BaseModel)


class DictState(BaseModel):
    """默认的宽松状态，允许任意 key-value。"""

    model_config = ConfigDict(extra="allow")


@dataclass
class TraceEvent:
    """一次执行事件的记录。"""

    node: str
    event: str  # "start" | "end" | "tool_call" | "handoff" | "error"
    timestamp: float
    data: dict = field(default_factory=dict)


@dataclass
class RunContext(Generic[StateT, DepsT]):
    """贯穿整个图执行的共享上下文。

    两个泛型参数：
    - StateT: 共享状态结构，节点间传递数据的唯一通道。
    - DepsT: 外部依赖，由使用者定义，框架只负责传递。
    """

    input: str
    state: StateT
    deps: DepsT
    trace: list[TraceEvent] = field(default_factory=list)
    current_agent: str = ""
    depth: int = 0
