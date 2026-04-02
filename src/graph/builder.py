"""GraphBuilder — 声明式图构建器。"""

from __future__ import annotations

from typing import Awaitable, Callable

from src.graph.types import (
    CompiledGraph,
    Edge,
    FunctionNode,
    GraphNode,
    NodeResult,
    ParallelGroup,
)


class GraphBuilder:
    """声明式图构建器，支持链式调用。"""

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[Edge] = []
        self._parallel_groups: list[ParallelGroup] = []
        self._entry: Optional[str] = None

    def add_node(self, node: GraphNode) -> GraphBuilder:
        """添加任意 GraphNode 实现。"""
        self._nodes[node.name] = node
        return self

    def add_function(self, name: str, fn: Callable[..., Awaitable[NodeResult]]) -> GraphBuilder:
        """添加一个函数节点。"""
        self._nodes[name] = FunctionNode(name=name, fn=fn)
        return self

    def set_entry(self, name: str) -> GraphBuilder:
        """设置入口节点。"""
        self._entry = name
        return self

    def add_edge(
        self,
        source: str,
        target: str,
        condition: str | None = None,
    ) -> GraphBuilder:
        """添加一条边（可选字符串条件，匹配 DecisionNode 的 chosen_branch）。"""
        self._edges.append(Edge(from_node=source, to_node=target, condition=condition))
        return self

    def add_parallel(self, nodes: list[str], then: str) -> GraphBuilder:
        """声明一组并行执行的节点，完成后汇聚到 then 节点。"""
        self._parallel_groups.append(ParallelGroup(nodes=list(nodes), then=then))
        return self

    def compile(self) -> CompiledGraph:
        """编译图：验证合法性后返回 CompiledGraph。"""
        if self._entry is None:
            raise ValueError("Graph has no entry node. Call set_entry() before compile().")
        if self._entry not in self._nodes:
            raise ValueError(f"Entry node '{self._entry}' not found in registered nodes.")

        for edge in self._edges:
            if edge.from_node not in self._nodes:
                raise ValueError(f"Edge source '{edge.from_node}' not found in registered nodes.")
            if edge.to_node not in self._nodes:
                raise ValueError(f"Edge target '{edge.to_node}' not found in registered nodes.")

        for pg in self._parallel_groups:
            for node_name in pg.nodes:
                if node_name not in self._nodes:
                    raise ValueError(
                        f"Parallel group node '{node_name}' not found in registered nodes."
                    )
            if pg.then not in self._nodes:
                raise ValueError(
                    f"Parallel group 'then' node '{pg.then}' not found in registered nodes."
                )

        return CompiledGraph(
            nodes=dict(self._nodes),
            edges=list(self._edges),
            entry=self._entry,
            parallel_groups=list(self._parallel_groups),
        )
