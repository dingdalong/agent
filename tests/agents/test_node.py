"""AgentNode 测试 — 从 context.deps.runner 获取 runner。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.agent import Agent, AgentResult
from src.agents.context import RunContext, DynamicState
from src.agents.deps import AgentDeps
from src.agents.node import AgentNode
from src.graph.types import NodeResult


@pytest.mark.asyncio
async def test_agent_node_uses_runner_from_context():
    """AgentNode 应从 context.deps.runner 获取 runner。"""
    agent = Agent(name="test", description="Test", instructions="test")
    mock_runner = AsyncMock()
    mock_runner.run = AsyncMock(return_value=AgentResult(text="ok", data={}))

    ctx = RunContext(
        input="hello",
        state=DynamicState(),
        deps=AgentDeps(runner=mock_runner),
    )
    node = AgentNode(agent=agent)
    result = await node.execute(ctx)

    mock_runner.run.assert_called_once_with(agent, ctx)
    assert result.output == {"text": "ok", "data": {}}


@pytest.mark.asyncio
async def test_agent_node_raises_when_no_runner():
    """deps.runner 为 None 时应抛出 RuntimeError。"""
    agent = Agent(name="test", description="Test", instructions="test")
    ctx = RunContext(
        input="hello",
        state=DynamicState(),
        deps=AgentDeps(),  # runner=None
    )
    node = AgentNode(agent=agent)
    with pytest.raises(RuntimeError, match="deps.runner is None"):
        await node.execute(ctx)
