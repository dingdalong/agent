"""AgentHooks 测试。"""
import pytest

from src.agents.deps import AgentDeps


@pytest.mark.asyncio
async def test_agent_hooks_on_start():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent
    from src.agents.context import RunContext, DynamicState

    calls = []

    async def on_start(agent, ctx):
        calls.append(("start", agent.name))

    hooks = AgentHooks(on_start=on_start)
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DynamicState(), deps=AgentDeps())

    await hooks.on_start(agent, ctx)
    assert calls == [("start", "test")]


@pytest.mark.asyncio
async def test_agent_hooks_on_end():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent, AgentResult
    from src.agents.context import RunContext, DynamicState

    calls = []

    async def on_end(agent, ctx, result):
        calls.append(("end", result.text))

    hooks = AgentHooks(on_end=on_end)
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DynamicState(), deps=AgentDeps())
    result = AgentResult(text="done")

    await hooks.on_end(agent, ctx, result)
    assert calls == [("end", "done")]


@pytest.mark.asyncio
async def test_agent_hooks_none_is_noop():
    from src.agents.hooks import AgentHooks
    from src.agents.agent import Agent
    from src.agents.context import RunContext, DynamicState

    hooks = AgentHooks()  # all None
    agent = Agent(name="test", description="test", instructions="test")
    ctx = RunContext(input="hi", state=DynamicState(), deps=AgentDeps())

    # Should not raise
    await hooks.on_start(agent, ctx)
