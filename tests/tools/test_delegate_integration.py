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
from src.tools.delegate import DelegateToolProvider
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

    # Agent_A (tool_terminal) 的 LLM 响应序列：
    # 1. 先调用 delegate_tool_calc(task="计算 1+1")
    # 2. 收到结果后输出最终回答
    #
    # Agent_B (tool_calc) 的 LLM 响应：
    # 1. 直接返回 "2"（不调用工具）
    call_count = 0

    async def mock_chat(messages, tools=None, silent=True):
        nonlocal call_count
        call_count += 1
        # 第一次调用来自 Agent_A — 决定 delegate
        if call_count == 1:
            return LLMResponse(
                content="",
                tool_calls={0: {
                    "id": "call_delegate",
                    "name": "delegate_tool_calc",
                    "arguments": json.dumps({"task": "计算 1+1"}),
                }},
            )
        # 第二次调用来自 Agent_B (被 delegate) — 直接返回结果
        if call_count == 2:
            return LLMResponse(content="计算结果是 2", tool_calls={})
        # 第三次调用来自 Agent_A — 收到 delegate 结果后输出
        return LLMResponse(content="命令执行完毕，1+1=2", tool_calls={})

    mock_llm.chat = mock_chat

    runner = AgentRunner(registry=registry)
    router = ToolRouter()

    delegate_provider = DelegateToolProvider(
        resolver=resolver,
        runner=runner,
        registry=registry,
        deps=AgentDeps(llm=mock_llm, tool_router=router),
    )
    router.add_provider(delegate_provider)

    # 获取 Agent_A (lazy-loaded, includes delegate tools)
    agent_a = registry.get("tool_terminal")
    assert "delegate_tool_calc" in agent_a.tools

    ctx = RunContext(
        input="执行计算任务",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=router),
        delegate_depth=0,
    )
    result = await runner.run(agent_a, ctx)

    assert "2" in result.text
    assert call_count == 3  # A调用1次 + B调用1次 + A调用1次


@pytest.mark.asyncio
async def test_delegated_agent_cannot_delegate_further(resolver, registry):
    """被 delegate 调用的 Agent_B 不应看到任何 delegate 工具。"""
    mock_llm = AsyncMock()

    tools_seen_by_b = []

    async def mock_chat(messages, tools=None, silent=True):
        # 记录传给 LLM 的工具列表
        if tools:
            tools_seen_by_b.extend([t["function"]["name"] for t in tools])
        return LLMResponse(content="结果", tool_calls={})

    mock_llm.chat = mock_chat

    runner = AgentRunner(registry=registry)
    router = ToolRouter()

    deps = AgentDeps(llm=mock_llm, tool_router=router)
    delegate_provider = DelegateToolProvider(
        resolver=resolver,
        runner=runner,
        registry=registry,
        deps=deps,
    )
    router.add_provider(delegate_provider)

    agent_b = registry.get("tool_calc")

    # 模拟被 delegate 调用（depth=1）
    ctx = RunContext(
        input="计算 1+1",
        state=DynamicState(),
        deps=deps,
        delegate_depth=1,
    )
    await runner.run(agent_b, ctx)

    # Agent_B 应只看到自己的工具，不含任何 delegate 工具
    delegate_tools = [t for t in tools_seen_by_b if t.startswith("delegate_")]
    assert delegate_tools == [], f"Agent_B should not see delegate tools, but saw: {delegate_tools}"
