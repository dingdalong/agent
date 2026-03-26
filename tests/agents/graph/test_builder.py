"""GraphBuilder 编译 + 验证测试。"""
import pytest
from src.agents.agent import Agent
from src.agents.graph.types import NodeResult


@pytest.fixture
def simple_agent():
    return Agent(name="agent_a", description="Agent A", instructions="Do A.")


@pytest.fixture
def another_agent():
    return Agent(name="agent_b", description="Agent B", instructions="Do B.")


async def dummy_fn(ctx):
    return NodeResult(output="done")


def test_builder_add_agent_and_compile(simple_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.set_entry("agent_a")
    compiled = graph.compile()
    assert compiled.entry == "agent_a"
    assert "agent_a" in compiled.nodes


def test_builder_add_function_and_compile():
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.set_entry("fn_a")
    compiled = graph.compile()
    assert "fn_a" in compiled.nodes


def test_builder_add_edge(simple_agent, another_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.add_agent("agent_b", another_agent)
    graph.set_entry("agent_a")
    graph.add_edge("agent_a", "agent_b")
    compiled = graph.compile()
    assert len(compiled.edges) == 1
    assert compiled.edges[0].source == "agent_a"
    assert compiled.edges[0].target == "agent_b"


def test_builder_add_conditional_edge(simple_agent, another_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.add_agent("agent_b", another_agent)
    graph.set_entry("agent_a")
    graph.add_edge("agent_a", "agent_b", condition=lambda ctx: True)
    compiled = graph.compile()
    assert compiled.edges[0].condition is not None


def test_builder_add_parallel(simple_agent, another_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.add_agent("agent_b", another_agent)
    graph.add_function("merge", dummy_fn)
    graph.set_entry("agent_a")
    graph.add_parallel(["agent_a", "agent_b"], then="merge")
    compiled = graph.compile()
    assert len(compiled.parallel_groups) == 1
    assert compiled.parallel_groups[0].nodes == ["agent_a", "agent_b"]
    assert compiled.parallel_groups[0].then == "merge"


def test_builder_chain_api(simple_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    result = graph.add_agent("agent_a", simple_agent)
    assert result is graph  # returns self for chaining


def test_compile_fails_without_entry(simple_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    with pytest.raises(ValueError, match="entry"):
        graph.compile()


def test_compile_fails_with_unknown_entry():
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.set_entry("nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_edge_target(simple_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.set_entry("agent_a")
    graph.add_edge("agent_a", "nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_parallel_node(simple_agent):
    from src.agents.graph.builder import GraphBuilder

    graph = GraphBuilder()
    graph.add_agent("agent_a", simple_agent)
    graph.add_function("merge", dummy_fn)
    graph.set_entry("agent_a")
    graph.add_parallel(["agent_a", "nonexistent"], then="merge")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()
