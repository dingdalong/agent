"""GraphBuilder 编译 + 验证测试。"""
import pytest

from src.graph.types import NodeResult, FunctionNode, GraphNode
from src.graph.builder import GraphBuilder


async def dummy_fn(ctx):
    return NodeResult(output="done")


async def another_fn(ctx):
    return NodeResult(output="another")


class CustomNode:
    """A custom GraphNode implementation for testing add_node()."""

    def __init__(self, name: str):
        self.name = name

    async def execute(self, context) -> NodeResult:
        return NodeResult(output=f"{self.name}_output")


def test_builder_add_function_and_compile():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.set_entry("fn_a")
    compiled = graph.compile()
    assert "fn_a" in compiled.nodes
    assert compiled.entry == "fn_a"


def test_builder_add_node_and_compile():
    node = CustomNode(name="custom_a")
    graph = GraphBuilder()
    graph.add_node(node)
    graph.set_entry("custom_a")
    compiled = graph.compile()
    assert "custom_a" in compiled.nodes
    assert compiled.entry == "custom_a"


def test_builder_add_node_satisfies_protocol():
    node = CustomNode(name="custom_a")
    assert isinstance(node, GraphNode)


def test_builder_add_edge():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_function("fn_b", another_fn)
    graph.set_entry("fn_a")
    graph.add_edge("fn_a", "fn_b")
    compiled = graph.compile()
    assert len(compiled.edges) == 1
    assert compiled.edges[0].from_node == "fn_a"
    assert compiled.edges[0].to_node == "fn_b"


def test_builder_add_conditional_edge():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_function("fn_b", another_fn)
    graph.set_entry("fn_a")
    graph.add_edge("fn_a", "fn_b", condition="yes")
    compiled = graph.compile()
    assert compiled.edges[0].condition == "yes"


def test_builder_add_parallel():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_function("fn_b", another_fn)
    graph.add_function("merge", dummy_fn)
    graph.set_entry("fn_a")
    graph.add_parallel(["fn_a", "fn_b"], then="merge")
    compiled = graph.compile()
    assert len(compiled.parallel_groups) == 1
    assert compiled.parallel_groups[0].nodes == ["fn_a", "fn_b"]
    assert compiled.parallel_groups[0].then == "merge"


def test_builder_chain_api():
    """All builder methods should return self for chaining."""
    graph = GraphBuilder()
    result = graph.add_function("fn_a", dummy_fn)
    assert result is graph

    result = graph.set_entry("fn_a")
    assert result is graph

    graph.add_function("fn_b", another_fn)
    result = graph.add_edge("fn_a", "fn_b")
    assert result is graph

    result = graph.add_parallel(["fn_a", "fn_b"], then="fn_a")
    assert result is graph

    node = CustomNode(name="custom")
    result = graph.add_node(node)
    assert result is graph


def test_builder_mixed_node_types():
    """Builder should accept both FunctionNode (via add_function) and custom nodes (via add_node)."""
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_node(CustomNode(name="custom_b"))
    graph.set_entry("fn_a")
    graph.add_edge("fn_a", "custom_b")
    compiled = graph.compile()
    assert "fn_a" in compiled.nodes
    assert "custom_b" in compiled.nodes


def test_compile_fails_without_entry():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    with pytest.raises(ValueError, match="entry"):
        graph.compile()


def test_compile_fails_with_unknown_entry():
    graph = GraphBuilder()
    graph.set_entry("nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_edge_source():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.set_entry("fn_a")
    graph.add_edge("nonexistent", "fn_a")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_edge_target():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.set_entry("fn_a")
    graph.add_edge("fn_a", "nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_parallel_node():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_function("merge", dummy_fn)
    graph.set_entry("fn_a")
    graph.add_parallel(["fn_a", "nonexistent"], then="merge")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()


def test_compile_fails_with_unknown_parallel_then():
    graph = GraphBuilder()
    graph.add_function("fn_a", dummy_fn)
    graph.add_function("fn_b", another_fn)
    graph.set_entry("fn_a")
    graph.add_parallel(["fn_a", "fn_b"], then="nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        graph.compile()
