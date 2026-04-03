"""跨分类委派集成测试。"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.agent import Agent, AgentResult
from src.agents.context import RunContext, DynamicState
from src.agents.deps import AgentDeps
from src.agents.registry import AgentRegistry
from src.agents.runner import AgentRunner
from src.tools.categories import CategoryResolver
from src.agents.delegate import DelegateToolProvider
from src.tools.router import ToolRouter
from src.llm.types import LLMResponse


@pytest.fixture
def categories():
    return {
        "tool_terminal": {"description": "终端操作", "tools": {"exec_cmd": "Execute command"}},
        "tool_calc": {"description": "数学计算", "tools": {"calculate": "Calculate math"}},
    }


@pytest.fixture
def resolver(categories):
    return CategoryResolver(categories)


@pytest.fixture
def registry(resolver):
    reg = AgentRegistry()
    reg.set_category_resolver(resolver)
    return reg


@pytest.mark.asyncio
async def test_delegate_end_to_end(resolver, registry):
    """Agent_A 通过 delegate 调用 Agent_B，获取结果并继续完成任务。"""
    mock_llm = AsyncMock()

    call_count = 0

    async def mock_chat(messages, tools=None, silent=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                content="",
                tool_calls={0: {
                    "id": "call_delegate",
                    "name": "delegate_tool_calc",
                    "arguments": json.dumps({
                        "objective": "执行计算任务，获取 1+1 的结果",
                        "task": "计算 1+1",
                        "context": "用户要求执行计算任务",
                        "expected_result": "计算结果数值",
                    }),
                }},
            )
        if call_count == 2:
            return LLMResponse(content="计算结果是 2", tool_calls={})
        return LLMResponse(content="命令执行完毕，1+1=2", tool_calls={})

    mock_llm.chat = mock_chat

    runner = AgentRunner()
    router = ToolRouter()

    # DelegateToolProvider 只接收 resolver
    delegate_provider = DelegateToolProvider(
        resolver=resolver,
    )
    router.add_provider(delegate_provider)

    agent_a = registry.get("tool_terminal")
    assert "delegate_tool_calc" in agent_a.tools

    # runner 和 registry 通过 deps 传递
    ctx = RunContext(
        input="执行计算任务",
        state=DynamicState(),
        deps=AgentDeps(
            llm=mock_llm,
            tool_router=router,
            agent_registry=registry,
            runner=runner,
        ),
        delegate_depth=0,
    )
    result = await runner.run(agent_a, ctx)

    assert "2" in result.text
    assert call_count == 3


@pytest.mark.asyncio
async def test_delegated_agent_cannot_delegate_further(resolver, registry):
    """被 delegate 调用的 Agent_B 不应看到任何 delegate 工具。"""
    mock_llm = AsyncMock()

    tools_seen_by_b = []

    async def mock_chat(messages, tools=None, silent=True):
        if tools:
            tools_seen_by_b.extend([t["function"]["name"] for t in tools])
        return LLMResponse(content="结果", tool_calls={})

    mock_llm.chat = mock_chat

    runner = AgentRunner()
    router = ToolRouter()

    deps = AgentDeps(
        llm=mock_llm,
        tool_router=router,
        agent_registry=registry,
        runner=runner,
    )
    delegate_provider = DelegateToolProvider(
        resolver=resolver,
    )
    router.add_provider(delegate_provider)

    agent_b = registry.get("tool_calc")

    ctx = RunContext(
        input="计算 1+1",
        state=DynamicState(),
        deps=deps,
        delegate_depth=1,
    )
    await runner.run(agent_b, ctx)

    delegate_tools = [t for t in tools_seen_by_b if t.startswith("delegate_")]
    assert delegate_tools == [], f"Agent_B should not see delegate tools, but saw: {delegate_tools}"


@pytest.mark.asyncio
async def test_delegate_execute_without_context_returns_error(resolver):
    """context=None 时应返回错误信息。"""
    provider = DelegateToolProvider(resolver=resolver)
    result = await provider.execute(
        "delegate_tool_calc",
        {"objective": "test", "task": "test"},
        context=None,
    )
    assert "错误" in result
