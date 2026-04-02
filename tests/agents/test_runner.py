# tests/agents/test_runner.py
"""AgentRunner 测试 — mock deps.llm.chat 和 ToolRouter。"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.agent import Agent, AgentResult, HandoffRequest
from src.agents.context import RunContext, DynamicState
from src.agents.deps import AgentDeps
from src.agents.registry import AgentRegistry
from src.llm.types import LLMResponse


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    return llm


@pytest.fixture
def mock_router():
    router = AsyncMock()
    router.route = AsyncMock(return_value="tool result")
    router.get_all_schemas = MagicMock(return_value=[
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ])
    return router


@pytest.fixture
def registry():
    reg = AgentRegistry()
    reg.register(Agent(
        name="calendar_agent",
        description="管理日历",
        instructions="日历专家。",
    ))
    return reg


@pytest.fixture
def simple_agent():
    return Agent(
        name="test_agent",
        description="Test",
        instructions="You are a test agent.",
        tools=["get_weather"],
    )


@pytest.fixture
def handoff_agent():
    return Agent(
        name="orchestrator",
        description="Orchestrator",
        instructions="You orchestrate.",
        handoffs=["calendar_agent"],
    )


@pytest.mark.asyncio
async def test_runner_simple_response(simple_agent, mock_router, mock_llm):
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(return_value=LLMResponse(content="Hello back!", tool_calls={}))

    ctx = RunContext(
        input="hello",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
    )

    runner = AgentRunner()
    result = await runner.run(simple_agent, ctx)

    assert result.text == "Hello back!"
    assert result.handoff is None


@pytest.mark.asyncio
async def test_runner_tool_call_loop(simple_agent, mock_router, mock_llm):
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls={0: {"id": "call_1", "name": "get_weather", "arguments": '{"city": "Beijing"}'}},
        ),
        LLMResponse(content="Beijing is sunny, 25°C.", tool_calls={}),
    ])

    ctx = RunContext(
        input="weather in Beijing",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
    )

    runner = AgentRunner()
    result = await runner.run(simple_agent, ctx)

    assert "25" in result.text
    # route 现在接收 3 个参数：name, args, context
    mock_router.route.assert_called_once_with("get_weather", {"city": "Beijing"}, ctx)


@pytest.mark.asyncio
async def test_runner_handoff_detection(handoff_agent, mock_router, mock_llm, registry):
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(return_value=LLMResponse(
        content="",
        tool_calls={0: {
            "id": "call_1",
            "name": "transfer_to_calendar_agent",
            "arguments": json.dumps({"task": "Book meeting tomorrow"}),
        }},
    ))

    ctx = RunContext(
        input="book a meeting",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router, agent_registry=registry),
    )

    runner = AgentRunner()
    result = await runner.run(handoff_agent, ctx)

    assert result.handoff is not None
    assert result.handoff.target == "calendar_agent"
    assert result.handoff.message.task == "Book meeting tomorrow"


@pytest.mark.asyncio
async def test_runner_max_rounds(simple_agent, mock_router, mock_llm):
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls={0: {"id": "call_1", "name": "get_weather", "arguments": "{}"}},
        ),
        LLMResponse(
            content="",
            tool_calls={0: {"id": "call_2", "name": "get_weather", "arguments": "{}"}},
        ),
        LLMResponse(content="Fallback response after max rounds", tool_calls={}),
    ])

    ctx = RunContext(
        input="loop",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
    )

    runner = AgentRunner(max_tool_rounds=2)
    result = await runner.run(simple_agent, ctx)

    assert mock_llm.chat.call_count == 3  # 2 rounds + 1 final
    assert result.text == "Fallback response after max rounds"


@pytest.mark.asyncio
async def test_runner_dynamic_instructions(mock_router, mock_llm):
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(return_value=LLMResponse(content="OK", tool_calls={}))

    def make_instructions(ctx):
        return f"Handle input: {ctx.input}"

    agent = Agent(
        name="dynamic",
        description="Dynamic",
        instructions=make_instructions,
    )
    ctx = RunContext(
        input="test input",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
    )

    runner = AgentRunner()
    await runner.run(agent, ctx)

    messages = mock_llm.chat.call_args[0][0]
    assert "Handle input: test input" in messages[0]["content"]


@pytest.mark.asyncio
async def test_runner_passes_context_to_route(mock_llm):
    """runner 应在 tool_router.route() 调用时透传 context。"""
    from src.agents.runner import AgentRunner

    mock_llm.chat = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls={0: {"id": "call_1", "name": "delegate_tool_calc", "arguments": '{"task": "1+1"}'}},
        ),
        LLMResponse(content="2", tool_calls={}),
    ])

    mock_router = AsyncMock()
    mock_router.route = AsyncMock(return_value="2")
    mock_router.get_all_schemas = MagicMock(return_value=[
        {
            "type": "function",
            "function": {
                "name": "delegate_tool_calc",
                "description": "委派任务给计算专家",
                "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
            },
        }
    ])

    agent = Agent(
        name="tool_terminal",
        description="终端操作",
        instructions="终端专家。",
        tools=["delegate_tool_calc"],
    )
    ctx = RunContext(
        input="calculate 1+1",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
        delegate_depth=0,
    )

    runner = AgentRunner()
    await runner.run(agent, ctx)

    # 验证 route 被调用时传入了 context
    call_args = mock_router.route.call_args
    assert call_args[0][0] == "delegate_tool_calc"       # tool_name
    assert call_args[0][1] == {"task": "1+1"}             # arguments
    assert call_args[0][2] is ctx                         # context


def test_build_tools_includes_delegates_at_depth_0():
    """delegate_depth=0 时，_build_tools 应包含 delegate 工具。"""
    from src.agents.runner import AgentRunner

    mock_router = MagicMock()
    mock_router.get_all_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": "exec", "description": "Execute", "parameters": {}}},
        {"type": "function", "function": {"name": "delegate_tool_calc", "description": "委派计算", "parameters": {}}},
    ])

    agent = Agent(name="test", description="Test", instructions="Test.", tools=["exec", "delegate_tool_calc"])
    ctx = RunContext(input="test", state=DynamicState(), deps=AgentDeps(tool_router=mock_router), delegate_depth=0)

    runner = AgentRunner()
    tools = runner._build_tools(agent, ctx)

    names = [t["function"]["name"] for t in tools]
    assert "exec" in names
    assert "delegate_tool_calc" in names


def test_build_tools_excludes_delegates_at_depth_1():
    """delegate_depth>=1 时，_build_tools 应过滤掉所有 delegate 工具。"""
    from src.agents.runner import AgentRunner

    mock_router = MagicMock()
    mock_router.get_all_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": "exec", "description": "Execute", "parameters": {}}},
        {"type": "function", "function": {"name": "delegate_tool_calc", "description": "委派计算", "parameters": {}}},
    ])

    agent = Agent(name="test", description="Test", instructions="Test.", tools=["exec", "delegate_tool_calc"])
    ctx = RunContext(input="test", state=DynamicState(), deps=AgentDeps(tool_router=mock_router), delegate_depth=1)

    runner = AgentRunner()
    tools = runner._build_tools(agent, ctx)

    names = [t["function"]["name"] for t in tools]
    assert "exec" in names
    assert "delegate_tool_calc" not in names


def test_build_handoff_tools_uses_context_registry():
    """_build_handoff_tools 应从 context.deps.agent_registry 获取 agent 描述。"""
    from src.agents.runner import AgentRunner

    registry = AgentRegistry()
    registry.register(Agent(name="target", description="目标 agent", instructions=""))
    agent = Agent(name="test", description="Test", instructions="", handoffs=["target"])
    ctx = RunContext(
        input="test",
        state=DynamicState(),
        deps=AgentDeps(agent_registry=registry),
    )

    runner = AgentRunner()
    tools = runner._build_handoff_tools(agent, ctx)

    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "transfer_to_target"
    assert "目标 agent" in tools[0]["function"]["description"]


@pytest.mark.asyncio
async def test_runner_emits_tool_events(simple_agent, mock_router, mock_llm):
    """runner 应通过 EventBus 发出 ToolCalled 和 ToolResult 事件。"""
    import asyncio
    from src.agents.runner import AgentRunner
    from src.events.bus import EventBus
    from src.events.levels import EventLevel

    mock_llm.chat = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls={0: {"id": "call_1", "name": "get_weather", "arguments": '{"city": "Beijing"}'}},
        ),
        LLMResponse(content="Sunny", tool_calls={}),
    ])

    bus = EventBus(level=EventLevel.DETAIL)
    received = []

    async def consumer():
        async for event in bus.subscribe():
            received.append(event)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    ctx = RunContext(
        input="weather",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router),
    )

    runner = AgentRunner(event_bus=bus)
    await runner.run(simple_agent, ctx)

    bus.close()
    await task

    type_names = [e.type for e in received]
    assert "agent_started" in type_names
    assert "tool_called" in type_names
    assert "tool_result" in type_names
    assert "agent_ended" in type_names


@pytest.mark.asyncio
async def test_runner_emits_handoff_event(handoff_agent, mock_router, mock_llm, registry):
    """runner 应通过 EventBus 发出 Handoff 事件。"""
    import asyncio
    from src.agents.runner import AgentRunner
    from src.events.bus import EventBus
    from src.events.levels import EventLevel

    mock_llm.chat = AsyncMock(return_value=LLMResponse(
        content="",
        tool_calls={0: {
            "id": "call_1",
            "name": "transfer_to_calendar_agent",
            "arguments": json.dumps({"task": "Book meeting"}),
        }},
    ))

    bus = EventBus(level=EventLevel.DETAIL)
    received = []

    async def consumer():
        async for event in bus.subscribe():
            received.append(event)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    ctx = RunContext(
        input="book a meeting",
        state=DynamicState(),
        deps=AgentDeps(llm=mock_llm, tool_router=mock_router, agent_registry=registry),
    )

    runner = AgentRunner(event_bus=bus)
    await runner.run(handoff_agent, ctx)

    bus.close()
    await task

    type_names = [e.type for e in received]
    assert "handoff" in type_names
    handoff_events = [e for e in received if e.type == "handoff"]
    assert handoff_events[0].to_agent == "calendar_agent"


def test_build_tools_includes_system_tools_when_agent_has_tools():
    """agent.tools 非空时，_build_tools 应自动包含系统工具 ask_user。"""
    from src.agents.runner import AgentRunner

    mock_router = MagicMock()
    mock_router.get_all_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": "exec", "description": "Execute", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user", "description": "Ask user", "parameters": {}}},
    ])

    agent = Agent(name="test", description="Test", instructions="Test.", tools=["exec"])
    ctx = RunContext(input="test", state=DynamicState(), deps=AgentDeps(tool_router=mock_router), delegate_depth=0)

    runner = AgentRunner()
    tools = runner._build_tools(agent, ctx)

    names = [t["function"]["name"] for t in tools]
    assert "exec" in names
    assert "ask_user" in names


def test_build_tools_includes_system_tools_when_agent_has_no_tools():
    """agent.tools 为空时，_build_tools 应只返回系统工具。"""
    from src.agents.runner import AgentRunner

    mock_router = MagicMock()
    mock_router.get_all_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": "exec", "description": "Execute", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user", "description": "Ask user", "parameters": {}}},
    ])

    agent = Agent(name="orchestrator", description="Orch", instructions="Route.", tools=[])
    ctx = RunContext(input="test", state=DynamicState(), deps=AgentDeps(tool_router=mock_router), delegate_depth=0)

    runner = AgentRunner()
    tools = runner._build_tools(agent, ctx)

    names = [t["function"]["name"] for t in tools]
    assert "ask_user" in names
    assert "exec" not in names


def test_build_tools_system_tools_not_filtered_by_delegate_depth():
    """delegate_depth >= 1 时，系统工具 ask_user 不应被过滤。"""
    from src.agents.runner import AgentRunner

    mock_router = MagicMock()
    mock_router.get_all_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": "exec", "description": "Execute", "parameters": {}}},
        {"type": "function", "function": {"name": "delegate_tool_calc", "description": "Delegate", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user", "description": "Ask user", "parameters": {}}},
    ])

    agent = Agent(name="test", description="Test", instructions="Test.", tools=["exec", "delegate_tool_calc"])
    ctx = RunContext(input="test", state=DynamicState(), deps=AgentDeps(tool_router=mock_router), delegate_depth=1)

    runner = AgentRunner()
    tools = runner._build_tools(agent, ctx)

    names = [t["function"]["name"] for t in tools]
    assert "exec" in names
    assert "ask_user" in names
    assert "delegate_tool_calc" not in names
