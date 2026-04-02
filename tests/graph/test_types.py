"""图类型测试 — GraphNode, FunctionNode, Edge, NodeResult, ParallelGroup, CompiledGraph。"""
import pytest
from dataclasses import dataclass, field
from pydantic import BaseModel, ConfigDict

from src.graph.types import (
    CompiledGraph,
    Edge,
    FunctionNode,
    GraphNode,
    NodeResult,
    ParallelGroup,
)


class SimpleState(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass
class SimpleContext:
    input: str = "test"
    state: SimpleState = field(default_factory=SimpleState)
    trace: list = field(default_factory=list)
    depth: int = 0


@pytest.fixture
def context():
    return SimpleContext(input="test")


@pytest.mark.asyncio
async def test_function_node_execute(context):
    async def greet(ctx):
        return NodeResult(output=f"Hello {ctx.input}")

    node = FunctionNode(name="greeter", fn=greet)
    assert node.name == "greeter"
    result = await node.execute(context)
    assert result.output == "Hello test"
    assert result.next is None
    assert result.handoff is None


@pytest.mark.asyncio
async def test_function_node_with_next(context):
    async def router_fn(ctx):
        return NodeResult(output="routed", next="target_node")

    node = FunctionNode(name="router", fn=router_fn)
    result = await node.execute(context)
    assert result.next == "target_node"


@pytest.mark.asyncio
async def test_function_node_with_list_next(context):
    async def fan_out(ctx):
        return NodeResult(output="fan", next=["a", "b"])

    node = FunctionNode(name="fan_out", fn=fan_out)
    result = await node.execute(context)
    assert result.next == ["a", "b"]


def test_node_result_defaults():
    result = NodeResult(output="data")
    assert result.output == "data"
    assert result.next is None
    assert result.handoff is None


def test_node_result_with_handoff():
    result = NodeResult(output="data", handoff={"target": "other", "task": "do it"})
    assert result.handoff == {"target": "other", "task": "do it"}


def test_edge_unconditional():
    edge = Edge(from_node="a", to_node="b")
    assert edge.from_node == "a"
    assert edge.to_node == "b"
    assert edge.condition is None


def test_edge_conditional():
    edge = Edge(from_node="a", to_node="b", condition="yes")
    assert edge.condition == "yes"


def test_parallel_group():
    pg = ParallelGroup(nodes=["a", "b"], then="c")
    assert pg.nodes == ["a", "b"]
    assert pg.then == "c"


def test_compiled_graph():
    async def noop(ctx):
        return NodeResult(output=None)

    node = FunctionNode(name="a", fn=noop)
    graph = CompiledGraph(
        nodes={"a": node},
        edges=[],
        entry="a",
        parallel_groups=[],
    )
    assert graph.entry == "a"
    assert "a" in graph.nodes


def test_function_node_satisfies_graph_node_protocol():
    """FunctionNode should satisfy the GraphNode protocol."""
    async def noop(ctx):
        return NodeResult(output=None)

    node = FunctionNode(name="test", fn=noop)
    assert isinstance(node, GraphNode)


def test_compiled_graph_default_parallel_groups():
    async def noop(ctx):
        return NodeResult(output=None)

    node = FunctionNode(name="a", fn=noop)
    graph = CompiledGraph(
        nodes={"a": node},
        edges=[],
        entry="a",
    )
    assert graph.parallel_groups == []
