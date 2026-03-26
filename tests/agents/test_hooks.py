"""AgentHooks + GraphHooks 测试。"""
import pytest


@pytest.mark.asyncio
async def test_agent_hooks_on_start():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent
    from src.agents.context import RunContext, DictState, EmptyDeps

    calls = []

    async def on_start(agent, ctx):
        calls.append(("start", agent.name))

    hooks = AgentHooks(on_start=on_start)
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DictState(), deps=EmptyDeps())

    await hooks.on_start(agent, ctx)
    assert calls == [("start", "test")]


@pytest.mark.asyncio
async def test_agent_hooks_on_end():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent, AgentResult
    from src.agents.context import RunContext, DictState, EmptyDeps

    calls = []

    async def on_end(agent, ctx, result):
        calls.append(("end", result.text))

    hooks = AgentHooks(on_end=on_end)
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DictState(), deps=EmptyDeps())
    result = AgentResult(text="done")

    await hooks.on_end(agent, ctx, result)
    assert calls == [("end", "done")]


@pytest.mark.asyncio
async def test_agent_hooks_none_is_noop():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent
    from src.agents.context import RunContext, DictState, EmptyDeps

    hooks = AgentHooks()  # all None
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DictState(), deps=EmptyDeps())

    # Should not raise
    await hooks.on_start(agent, ctx)


@pytest.mark.asyncio
async def test_graph_hooks_on_node_start():
    from src.agents.hooks import GraphHooks
    from src.agents.context import RunContext, DictState, EmptyDeps

    calls = []

    async def on_node_start(node_name, ctx):
        calls.append(node_name)

    hooks = GraphHooks(on_node_start=on_node_start)
    ctx = RunContext(input="hi", state=DictState(), deps=EmptyDeps())

    await hooks.on_node_start("weather", ctx)
    assert calls == ["weather"]


@pytest.mark.asyncio
async def test_graph_hooks_none_is_noop():
    from src.agents.hooks import GraphHooks
    from src.agents.context import RunContext, DictState, EmptyDeps

    hooks = GraphHooks()
    ctx = RunContext(input="hi", state=DictState(), deps=EmptyDeps())

    # Should not raise
    await hooks.on_graph_start(ctx)
    await hooks.on_node_start("x", ctx)
