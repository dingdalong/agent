"""图类型定义 — 节点、边、执行结果、编译后的图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable  # noqa: F401 — Callable used by FunctionNode


@dataclass
class NodeResult:
    """节点执行结果。"""
    output: Any
    next: Optional[str | list[str]] = None
    handoff: Any = None


@runtime_checkable
class GraphNode(Protocol):
    """图节点协议 — 所有可在 GraphEngine 中执行的节点必须实现此接口。

    实现者通过 execute() 接收 RunContext，返回 NodeResult 控制执行流：
    - output: 节点计算结果，写入 context.state
    - next: 显式指定下一个节点（覆盖边路由）
    - handoff: 请求切换到另一个智能体

    实现者：AgentNode（智能体）、FunctionNode（普通函数）。
    """
    name: str

    async def execute(self, context: Any) -> NodeResult: ...


class FunctionNode:
    """包装一个普通 async 函数。"""

    def __init__(self, name: str, fn: Callable[..., Awaitable[NodeResult]]):
        self.name = name
        self.fn = fn

    async def execute(self, context: Any) -> NodeResult:
        return await self.fn(context)


@dataclass
class Edge:
    """节点间的连接。condition 为字符串标签，用于匹配 DecisionNode 的 chosen_branch。"""
    from_node: str
    to_node: str
    condition: Optional[str] = None


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
