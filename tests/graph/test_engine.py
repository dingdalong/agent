"""GraphEngine 执行测试 — 顺序、并行、条件、handoff、EventBus、tracing。"""
import asyncio
import pytest
from dataclasses import dataclass, field
from typing import Any
from pydantic import BaseModel, ConfigDict

from src.graph.types import NodeResult, FunctionNode
from src.graph.builder import GraphBuilder
from src.graph.engine import GraphEngine, GraphResult
from src.graph.messages import AgentMessage
from src.events.bus import EventBus
from src.events.levels import EventLevel


# --- Test helpers: agent-agnostic context ---

class DynamicState(BaseModel):
    """宽松状态，允许任意 key-value。"""
    model_config = ConfigDict(extra="allow")


@dataclass
class SimpleContext:
    """Agent 无关的简单上下文，用于测试 GraphEngine。"""
    input: str = "test"
    state: DynamicState = field(default_factory=DynamicState)
    trace: list = field(default_factory=list)
    depth: int = 0


@dataclass
class HandoffData:
    """handoff 数据结构，用于测试。使用 AgentMessage。"""
    target: str
    message: Any = None  # AgentMessage

    @property
    def task(self):
        return self.message.task if self.message else ""


@pytest.fixture
def engine():
    return GraphEngine()


# --- Single node ---

@pytest.mark.asyncio
async def test_single_function_node(engine):
    async def greet(ctx):
        return NodeResult(output=f"Hello {ctx.input}")

    graph = GraphBuilder()
    graph.add_function("greet", greet)
    graph.set_entry("greet")
    compiled = graph.compile()

    ctx = SimpleContext(input="World")
    result = await engine.run(compiled, ctx)
    assert result.output == "Hello World"


# --- Sequential execution ---

@pytest.mark.asyncio
async def test_sequential_function_nodes(engine):
    async def step1(ctx):
        return NodeResult(output="step1_done")

    async def step2(ctx):
        return NodeResult(output=f"step2 got {ctx.state.step1}")

    graph = GraphBuilder()
    graph.add_function("step1", step1)
    graph.add_function("step2", step2)
    graph.set_entry("step1")
    graph.add_edge("step1", "step2")
    compiled = graph.compile()

    ctx = SimpleContext(input="test")
    result = await engine.run(compiled, ctx)
    assert result.output == "step2 got step1_done"


@pytest.mark.asyncio
async def test_three_node_chain(engine):
    async def a(ctx):
        return NodeResult(output="a_done")

    async def b(ctx):
        return NodeResult(output=f"b got {ctx.state.a}")

    async def c(ctx):
        return NodeResult(output=f"c got {ctx.state.b}")

    graph = (
        GraphBuilder()
        .add_function("a", a)
        .add_function("b", b)
        .add_function("c", c)
        .set_entry("a")
        .add_edge("a", "b")
        .add_edge("b", "c")
    )
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.output == "c got b got a_done"


# --- Explicit next routing ---

@pytest.mark.asyncio
async def test_explicit_next_routing(engine):
    async def router(ctx):
        return NodeResult(output="routed", next="target")

    async def target(ctx):
        return NodeResult(output="reached target")

    async def dead_end(ctx):
        return NodeResult(output="should not reach")

    graph = GraphBuilder()
    graph.add_function("router", router)
    graph.add_function("target", target)
    graph.add_function("dead_end", dead_end)
    graph.set_entry("router")
    graph.add_edge("router", "dead_end")  # edge should be ignored due to explicit next
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.output == "reached target"


# --- Conditional routing ---

@pytest.mark.asyncio
async def test_conditional_edge(engine):
    async def check(ctx):
        # 返回 chosen_branch 供条件边匹配
        return NodeResult(output={"chosen_branch": "go_a"})

    async def path_a(ctx):
        return NodeResult(output="took path A")

    async def path_b(ctx):
        return NodeResult(output="took path B")

    graph = GraphBuilder()
    graph.add_function("check", check)
    graph.add_function("path_a", path_a)
    graph.add_function("path_b", path_b)
    graph.set_entry("check")
    graph.add_edge("check", "path_a", condition="go_a")
    graph.add_edge("check", "path_b", condition="go_b")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.output == "took path A"


# --- Parallel execution ---

@pytest.mark.asyncio
async def test_parallel_execution(engine):
    async def task_a(ctx):
        return NodeResult(output="result_a")

    async def task_b(ctx):
        return NodeResult(output="result_b")

    async def merge(ctx):
        a = ctx.state.task_a
        b = ctx.state.task_b
        return NodeResult(output=f"merged: {a} + {b}")

    graph = GraphBuilder()
    graph.add_function("task_a", task_a)
    graph.add_function("task_b", task_b)
    graph.add_function("merge", merge)
    graph.set_entry("task_a")
    graph.add_parallel(["task_a", "task_b"], then="merge")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert "result_a" in result.output
    assert "result_b" in result.output


# --- Handoff ---

@pytest.mark.asyncio
async def test_handoff_to_graph_node(engine):
    """Test handoff where target is a node in the graph."""
    async def initiator(ctx):
        return NodeResult(
            output="initiating",
            handoff=HandoffData(target="handler", message=AgentMessage(objective="handle this", task="handle this")),
        )

    async def handler(ctx):
        return NodeResult(output=f"handled: {ctx.input}")

    graph = GraphBuilder()
    graph.add_function("initiator", initiator)
    graph.add_function("handler", handler)
    graph.set_entry("initiator")
    compiled = graph.compile()

    ctx = SimpleContext(input="original")
    result = await engine.run(compiled, ctx)
    assert result.output == "handled: handle this"
    assert ctx.depth == 1


@pytest.mark.asyncio
async def test_handoff_max_depth():
    """Test that handoff stops at max depth."""
    async def looper(ctx):
        return NodeResult(
            output="loop",
            handoff=HandoffData(target="looper", message=AgentMessage(objective="loop", task="loop again")),
        )

    graph = GraphBuilder()
    graph.add_function("looper", looper)
    graph.set_entry("looper")
    compiled = graph.compile()

    engine = GraphEngine(max_handoff_depth=3)
    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    # Should stop after max_handoff_depth
    assert ctx.depth > 3


@pytest.mark.asyncio
async def test_handoff_to_unknown_target(engine):
    """Test handoff to a target not in the graph logs error and continues."""
    async def initiator(ctx):
        return NodeResult(
            output="initiating",
            handoff=HandoffData(target="nonexistent", message=AgentMessage(objective="handle", task="handle")),
        )

    graph = GraphBuilder()
    graph.add_function("initiator", initiator)
    graph.set_entry("initiator")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    # Should complete without error but the handoff target is not found
    assert result.output == "initiating"


# --- Tracing ---

@pytest.mark.asyncio
async def test_trace_events_recorded(engine):
    async def step(ctx):
        return NodeResult(output="done")

    graph = GraphBuilder()
    graph.add_function("step", step)
    graph.set_entry("step")
    compiled = graph.compile()

    ctx = SimpleContext()
    await engine.run(compiled, ctx)

    start_events = [e for e in ctx.trace if e["event"] == "start"]
    end_events = [e for e in ctx.trace if e["event"] == "end"]
    assert len(start_events) >= 1
    assert len(end_events) >= 1
    assert start_events[0]["node"] == "step"


@pytest.mark.asyncio
async def test_trace_includes_timestamp(engine):
    async def step(ctx):
        return NodeResult(output="done")

    graph = GraphBuilder()
    graph.add_function("step", step)
    graph.set_entry("step")
    compiled = graph.compile()

    ctx = SimpleContext()
    await engine.run(compiled, ctx)

    for event in ctx.trace:
        assert "timestamp" in event
        assert isinstance(event["timestamp"], float)


@pytest.mark.asyncio
async def test_trace_on_error(engine):
    async def failing(ctx):
        raise RuntimeError("boom")

    graph = GraphBuilder()
    graph.add_function("failing", failing)
    graph.set_entry("failing")
    compiled = graph.compile()

    ctx = SimpleContext()
    with pytest.raises(RuntimeError, match="boom"):
        await engine.run(compiled, ctx)

    error_events = [e for e in ctx.trace if e["event"] == "error"]
    assert len(error_events) >= 1
    assert "boom" in error_events[0]["data"]["error"]


# --- GraphResult ---

@pytest.mark.asyncio
async def test_graph_result_contains_state(engine):
    async def step(ctx):
        return NodeResult(output="value")

    graph = GraphBuilder()
    graph.add_function("step", step)
    graph.set_entry("step")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.state is ctx.state
    assert result.output == "value"
    assert isinstance(result.trace, list)


# --- EventBus lifecycle ---

@pytest.mark.asyncio
async def test_event_bus_emits_lifecycle_events():
    """EventBus 应在图/节点开始和结束时发出事件。"""
    bus = EventBus(level=EventLevel.PROGRESS)
    received = []

    async def consumer():
        async for event in bus.subscribe():
            received.append(event)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    engine = GraphEngine(event_bus=bus)

    async def step(ctx):
        return NodeResult(output="done")

    graph = GraphBuilder()
    graph.add_function("step", step)
    graph.set_entry("step")
    compiled = graph.compile()

    ctx = SimpleContext()
    await engine.run(compiled, ctx)

    bus.close()
    await task

    type_names = [e.type for e in received]
    assert "graph_started" in type_names
    assert "graph_ended" in type_names
    assert "node_started" in type_names
    assert "node_ended" in type_names


@pytest.mark.asyncio
async def test_engine_without_event_bus():
    """不传 EventBus 时 engine 正常运行（不 emit 事件）。"""
    engine = GraphEngine()

    async def step(ctx):
        return NodeResult(output="done")

    graph = GraphBuilder()
    graph.add_function("step", step)
    graph.set_entry("step")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.output == "done"


# --- add_node integration ---

@pytest.mark.asyncio
async def test_engine_with_custom_node(engine):
    """Test engine can execute a custom GraphNode added via add_node()."""

    class MyNode:
        def __init__(self, name: str):
            self.name = name

        async def execute(self, context):
            return NodeResult(output=f"custom_{self.name}")

    graph = GraphBuilder()
    graph.add_node(MyNode(name="my_node"))
    graph.set_entry("my_node")
    compiled = graph.compile()

    ctx = SimpleContext()
    result = await engine.run(compiled, ctx)
    assert result.output == "custom_my_node"


# --- Pending-based parallel execution ---

@pytest.mark.asyncio
async def test_pending_parallel_nodes_execute_concurrently():
    """pending 中有多个节点时并行执行（通过 next 返回列表触发）。"""
    import asyncio

    execution_order = []

    async def start(ctx):
        return NodeResult(output="ok", next=["slow", "fast"])

    async def slow_node(ctx):
        execution_order.append("slow_start")
        await asyncio.sleep(0.05)
        execution_order.append("slow_end")
        return NodeResult(output="slow_done")

    async def fast_node(ctx):
        execution_order.append("fast_start")
        execution_order.append("fast_end")
        return NodeResult(output="fast_done")

    async def merge_node(ctx):
        return NodeResult(output="merged")

    builder = GraphBuilder()
    builder.add_function("start", start)
    builder.add_function("slow", slow_node)
    builder.add_function("fast", fast_node)
    builder.add_function("merge", merge_node)
    builder.set_entry("start")
    builder.add_edge("slow", "merge")
    builder.add_edge("fast", "merge")
    graph = builder.compile()

    engine = GraphEngine()
    ctx = SimpleContext()
    result = await engine.run(graph, ctx)

    # Both should start before slow finishes
    assert "fast_start" in execution_order
    assert "slow_start" in execution_order
    assert result.output == "merged"


# --- _last_output tracking ---

@pytest.mark.asyncio
async def test_last_output_written_to_state():
    """每个节点执行后 _last_output 被写入 state。"""
    async def node_fn(ctx):
        return NodeResult(output={"text": "hello", "data": {"k": 1}})

    builder = GraphBuilder()
    builder.add_function("only", node_fn)
    builder.set_entry("only")
    graph = builder.compile()

    engine = GraphEngine()
    ctx = SimpleContext()
    await engine.run(graph, ctx)

    assert hasattr(ctx.state, "_last_output")
    assert ctx.state._last_output == {"text": "hello", "data": {"k": 1}}
