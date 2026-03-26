"""图类型定义 — 节点、边、执行结果、编译后的图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from src.agents.agent import HandoffRequest


@dataclass
class NodeResult:
    """节点执行结果。"""

    output: Any
    next: Optional[str | list[str]] = None
    handoff: Optional[HandoffRequest] = None


@runtime_checkable
class GraphNode(Protocol):
    """图节点协议。"""

    name: str

    async def execute(self, context: Any) -> NodeResult: ...


class AgentNode:
    """包装一个 Agent，内部用 AgentRunner 驱动。"""

    def __init__(self, agent: Any, runner: Any = None):
        self.name: str = agent.name
        self.agent = agent
        self.runner = runner

    async def execute(self, context: Any) -> NodeResult:
        if self.runner is None:
            raise RuntimeError(f"AgentNode '{self.name}' has no runner assigned")
        result = await self.runner.run(self.agent, context)
        return NodeResult(
            output={"text": result.text, "data": result.data},
            handoff=result.handoff,
        )


class FunctionNode:
    """包装一个普通 async 函数。"""

    def __init__(self, name: str, fn: Callable[..., Awaitable[NodeResult]]):
        self.name = name
        self.fn = fn

    async def execute(self, context: Any) -> NodeResult:
        return await self.fn(context)


@dataclass
class Edge:
    """节点间的连接。"""

    source: str
    target: str
    condition: Optional[Callable[..., bool]] = None


@dataclass
class ParallelGroup:
    """一组需要并行执行的节点。"""

    nodes: list[str]
    then: str


@dataclass
class CompiledGraph:
    """编译后的图，不可变，可复用。"""

    nodes: dict[str, GraphNode]
    edges: list[Edge]
    entry: str
    parallel_groups: list[ParallelGroup] = field(default_factory=list)
