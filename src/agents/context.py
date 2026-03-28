"""RunContext — 贯穿图执行的泛型共享上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict


StateT = TypeVar("StateT", bound=BaseModel)
DepsT = TypeVar("DepsT", bound=BaseModel)


class DynamicState(BaseModel):
    """动态宽松状态，允许任意 key-value。

    用于 Plan/Skill 等场景，节点名在运行时才确定，
    GraphEngine._write_state 通过 setattr 动态写入。
    """

    model_config = ConfigDict(extra="allow")


class AppState(DynamicState):
    """主对话流程的状态，显式声明已知字段。

    继承 DynamicState 保留 extra="allow"，
    使 GraphEngine._write_state 仍可动态写入节点输出。
    """

    memory_context: str | None = None
    conversation_history: list[dict[str, Any]] | None = None


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
