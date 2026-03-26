"""图类型测试 — GraphNode, AgentNode, FunctionNode, Edge, NodeResult。"""
import pytest
from src.agents.agent import Agent, AgentResult
from src.agents.context import RunContext, DictState, EmptyDeps


@pytest.fixture
def context():
    return RunContext(input="test", state=DictState(), deps=EmptyDeps())


@pytest.mark.asyncio
async def test_function_node_execute(context):
    from src.agents.graph.types import FunctionNode, NodeResult

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
    from src.agents.graph.types import FunctionNode, NodeResult

    async def router_fn(ctx):
        return NodeResult(output="routed", next="target_node")

    node = FunctionNode(name="router", fn=router_fn)
    result = await node.execute(context)
    assert result.next == "target_node"


def test_node_result_defaults():
    from src.agents.graph.types import NodeResult

    result = NodeResult(output="data")
    assert result.output == "data"
    assert result.next is None
    assert result.handoff is None


def test_edge_unconditional():
    from src.agents.graph.types import Edge

    edge = Edge(source="a", target="b")
    assert edge.source == "a"
    assert edge.target == "b"
    assert edge.condition is None


def test_edge_conditional():
    from src.agents.graph.types import Edge

    edge = Edge(source="a", target="b", condition=lambda ctx: ctx.state.counter > 0)
    assert edge.condition is not None


def test_parallel_group():
    from src.agents.graph.types import ParallelGroup

    pg = ParallelGroup(nodes=["a", "b"], then="c")
    assert pg.nodes == ["a", "b"]
    assert pg.then == "c"


def test_compiled_graph():
    from src.agents.graph.types import CompiledGraph, FunctionNode, NodeResult, Edge

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
