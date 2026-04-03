"""DelegateToolProvider 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.tools.categories import CategoryResolver
from src.agents.registry import AgentRegistry
from src.agents.deps import AgentDeps
from src.graph.messages import AgentResponse


@pytest.fixture
def resolver():
    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute command"}},
        "tool_calc": {"description": "数学计算", "tools": {"calc": "Calculate math"}},
    }
    return CategoryResolver(cats)


@pytest.fixture
def mock_runner():
    return AsyncMock()


@pytest.fixture
def mock_registry(resolver):
    from src.agents.registry import AgentRegistry
    registry = AgentRegistry()
    registry.set_category_resolver(resolver)
    return registry


@pytest.fixture
def mock_deps(mock_runner, mock_registry):
    from src.agents.deps import AgentDeps
    return AgentDeps(runner=mock_runner, agent_registry=mock_registry)


@pytest.fixture
def mock_context(mock_deps):
    """构造一个带 deps 和 delegate_depth 的最小 context 对象。"""
    ctx = MagicMock()
    ctx.deps = mock_deps
    ctx.delegate_depth = 0
    return ctx


@pytest.fixture
def provider(resolver):
    from src.agents.delegate import DelegateToolProvider
    return DelegateToolProvider(resolver=resolver)


def test_can_handle(provider):
    assert provider.can_handle("delegate_tool_terminal") is True
    assert provider.can_handle("delegate_tool_calc") is True
    assert provider.can_handle("delegate_tool_unknown") is False
    assert provider.can_handle("tool_terminal") is False
    assert provider.can_handle("calculate") is False


def test_get_schemas(provider):
    schemas = provider.get_schemas()
    assert len(schemas) == 3  # 2 delegate + 1 parallel_delegate
    names = {s["function"]["name"] for s in schemas}
    assert names == {"delegate_tool_terminal", "delegate_tool_calc", "parallel_delegate"}
    terminal_schema = next(s for s in schemas if s["function"]["name"] == "delegate_tool_terminal")
    assert terminal_schema["type"] == "function"
    params = terminal_schema["function"]["parameters"]
    assert "objective" in params["properties"]
    assert "task" in params["properties"]
    assert set(params["required"]) == {"objective", "task"}


@pytest.mark.asyncio
async def test_execute_delegates_to_runner(provider, mock_runner, mock_context):
    from src.agents.agent import AgentResult
    mock_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="已完成\n执行完成", sender="tool_terminal")))
    result = await provider.execute("delegate_tool_terminal", {"task": "列出当前目录"}, context=mock_context)
    assert "执行完成" in result
    mock_runner.run.assert_called_once()
    call_args = mock_runner.run.call_args
    agent = call_args[0][0]
    assert agent.name == "tool_terminal"
    ctx = call_args[0][1]
    assert "具体任务：列出当前目录" in ctx.input


@pytest.mark.asyncio
async def test_execute_unknown_agent(provider, mock_context):
    # Replace registry with one that can't resolve the agent
    mock_context.deps.agent_registry = MagicMock()
    mock_context.deps.agent_registry.get = MagicMock(return_value=None)
    result = await provider.execute("delegate_tool_unknown", {"task": "test"}, context=mock_context)
    assert "错误" in result


@pytest.mark.asyncio
async def test_execute_ensures_mcp_connection():
    """execute 应在运行 agent 前确保 MCP server 已连接。"""
    from src.agents.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_files": {
            "description": "文件操作",
            "tools": {
                "mcp_desktop_commander_read_file": "Read file",
                "mcp_desktop_commander_write_file": "Write file",
            },
        },
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="done", sender="tool_files")))
    test_deps = AgentDeps(runner=test_runner, agent_registry=test_registry)

    mock_mcp = AsyncMock()
    mock_mcp.ensure_servers_for_tools = AsyncMock()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        mcp_manager=mock_mcp,
    )

    ctx = MagicMock()
    ctx.deps = test_deps
    ctx.delegate_depth = 0

    await provider.execute("delegate_tool_files", {"task": "read something"}, context=ctx)
    mock_mcp.ensure_servers_for_tools.assert_called_once_with([
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
    ])


@pytest.mark.asyncio
async def test_execute_no_mcp_manager_still_works():
    """没有 mcp_manager 时（纯本地工具）execute 仍正常工作。"""
    from src.agents.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="42", sender="tool_calc")))
    test_deps = AgentDeps(runner=test_runner, agent_registry=test_registry)

    provider = DelegateToolProvider(resolver=test_resolver)

    ctx = MagicMock()
    ctx.deps = test_deps
    ctx.delegate_depth = 0

    result = await provider.execute("delegate_tool_calc", {"task": "1+1"}, context=ctx)
    assert result == "42"


@pytest.mark.asyncio
async def test_execute_propagates_delegate_depth():
    """execute 创建的子 RunContext 应将 delegate_depth 递增。"""
    from src.agents.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="done", sender="tool_terminal")))
    test_deps = AgentDeps(runner=test_runner, agent_registry=test_registry)

    provider = DelegateToolProvider(resolver=test_resolver)

    ctx = MagicMock()
    ctx.deps = test_deps
    ctx.delegate_depth = 0

    await provider.execute("delegate_tool_terminal", {"task": "run ls"}, context=ctx)

    call_args = test_runner.run.call_args
    sub_ctx = call_args[0][1]
    assert sub_ctx.delegate_depth == 1


@pytest.mark.asyncio
async def test_execute_propagates_parent_delegate_depth():
    """当 delegate_depth 已非零时，子 RunContext 应继续递增。"""
    from src.agents.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="done", sender="tool_terminal")))
    test_deps = AgentDeps(runner=test_runner, agent_registry=test_registry)

    provider = DelegateToolProvider(resolver=test_resolver)

    ctx = MagicMock()
    ctx.deps = test_deps
    ctx.delegate_depth = 1

    await provider.execute("delegate_tool_terminal", {"task": "run ls"}, context=ctx)

    call_args = test_runner.run.call_args
    sub_ctx = call_args[0][1]
    assert sub_ctx.delegate_depth == 2


def test_format_for_receiver_all_fields():
    """所有字段都有值时，接收方 input 包含全部信息。"""
    from src.graph.messages import AgentMessage, format_for_receiver

    msg = AgentMessage(
        objective="判断明天是否适合去故宫",
        task="查询北京明天天气",
        context="用户计划明天去故宫",
        expected_result="天气状况、温度、出行建议",
    )
    result = format_for_receiver(msg)
    assert "最终目标：判断明天是否适合去故宫" in result
    assert "具体任务：查询北京明天天气" in result
    assert "相关上下文：用户计划明天去故宫" in result
    assert "期望结果：天气状况、温度、出行建议" in result
    assert "不要猜测或假设" in result
    assert "已完成 / 信息不足 / 失败" in result


def test_format_for_receiver_optional_fields_missing():
    """可选字段为空时，不显示对应行。"""
    from src.graph.messages import AgentMessage, format_for_receiver

    msg = AgentMessage(
        objective="帮用户查天气",
        task="查询天气预报",
        context="",
        expected_result=None,
    )
    result = format_for_receiver(msg)
    assert "最终目标：帮用户查天气" in result
    assert "具体任务：查询天气预报" in result
    assert "相关上下文" not in result
    assert "期望结果" not in result
    assert "不要猜测或假设" in result


def test_get_schemas_has_structured_fields(provider):
    """schema 应包含 objective/task/context/expected_result 四个字段。"""
    schemas = provider.get_schemas()
    terminal_schema = next(s for s in schemas if s["function"]["name"] == "delegate_tool_terminal")
    params = terminal_schema["function"]["parameters"]
    props = params["properties"]

    assert "objective" in props
    assert "task" in props
    assert "context" in props
    assert "expected_result" in props
    # objective 和 task 必填，context 和 expected_result 可选
    assert set(params["required"]) == {"objective", "task"}


def test_get_schemas_description_uses_template(provider):
    """schema description 应包含委托引导语。"""
    schemas = provider.get_schemas()
    terminal_schema = next(s for s in schemas if s["function"]["name"] == "delegate_tool_terminal")
    desc = terminal_schema["function"]["description"]
    assert "确保对方无需额外信息就能执行任务" in desc


@pytest.mark.asyncio
async def test_execute_builds_structured_input(provider, mock_runner, mock_context):
    """execute 应用接收方模板组装 input，包含 objective/task/context/expected_result。"""
    from src.agents.agent import AgentResult

    mock_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="已完成\n结果", sender="tool_terminal")))
    await provider.execute("delegate_tool_terminal", {
        "objective": "帮用户管理文件",
        "task": "列出当前目录",
        "context": "用户在 /home/user 目录下",
        "expected_result": "文件列表",
    }, context=mock_context)

    call_args = mock_runner.run.call_args
    ctx = call_args[0][1]
    assert "最终目标：帮用户管理文件" in ctx.input
    assert "具体任务：列出当前目录" in ctx.input
    assert "相关上下文：用户在 /home/user 目录下" in ctx.input
    assert "期望结果：文件列表" in ctx.input


@pytest.mark.asyncio
async def test_execute_optional_fields_omitted(provider, mock_runner, mock_context):
    """只传 objective 和 task 时，input 不包含 context 和 expected_result 行。"""
    from src.agents.agent import AgentResult

    mock_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="已完成\n结果", sender="tool_terminal")))
    await provider.execute("delegate_tool_terminal", {
        "objective": "查天气",
        "task": "查询天气预报",
    }, context=mock_context)

    call_args = mock_runner.run.call_args
    ctx = call_args[0][1]
    assert "最终目标：查天气" in ctx.input
    assert "具体任务：查询天气预报" in ctx.input
    assert "相关上下文" not in ctx.input
    assert "期望结果" not in ctx.input


@pytest.mark.asyncio
async def test_execute_backward_compat_task_only(provider, mock_runner, mock_context):
    """兼容旧格式：只传 task 时，objective 用 task 兜底。"""
    from src.agents.agent import AgentResult

    mock_runner.run = AsyncMock(return_value=AgentResult(response=AgentResponse(text="已完成\n42", sender="tool_terminal")))
    await provider.execute("delegate_tool_terminal", {"task": "计算 1+1"}, context=mock_context)

    call_args = mock_runner.run.call_args
    ctx = call_args[0][1]
    assert "具体任务：计算 1+1" in ctx.input


@pytest.mark.asyncio
async def test_execute_uses_engine_when_available():
    """deps 中同时有 graph_engine 和 runner 时，应通过 engine 执行。"""
    from src.agents.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult
    from src.graph.engine import GraphResult
    from pydantic import BaseModel, ConfigDict

    class MockState(BaseModel):
        model_config = ConfigDict(extra="allow")

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    mock_engine = AsyncMock()
    # engine.run 返回 GraphResult，其 output 是 AgentResponse
    mock_engine.run = AsyncMock(return_value=GraphResult(
        output=AgentResponse(text="engine执行完成", sender="tool_terminal"),
        state=MockState(),
        trace=[],
    ))
    test_deps = AgentDeps(
        runner=test_runner,
        agent_registry=test_registry,
        graph_engine=mock_engine,
    )

    provider = DelegateToolProvider(resolver=test_resolver)

    ctx = MagicMock()
    ctx.deps = test_deps
    ctx.delegate_depth = 0
    ctx.current_agent = "orchestrator"

    result = await provider.execute("delegate_tool_terminal", {
        "objective": "测试",
        "task": "执行命令",
    }, context=ctx)

    # 应通过 engine 而非直接 runner
    mock_engine.run.assert_called_once()
    test_runner.run.assert_not_called()
    assert result == "engine执行完成"


class TestParallelDelegate:
    def test_can_handle_parallel(self, provider):
        assert provider.can_handle("parallel_delegate") is True

    def test_parallel_schema_in_schemas(self, provider):
        schemas = provider.get_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "parallel_delegate" in names

    def test_parallel_schema_structure(self, provider):
        schemas = provider.get_schemas()
        parallel = next(s for s in schemas if s["function"]["name"] == "parallel_delegate")
        params = parallel["function"]["parameters"]
        assert "tasks" in params["properties"]
        items = params["properties"]["tasks"]["items"]
        assert "agent" in items["properties"]
        assert "objective" in items["properties"]
        assert "task" in items["properties"]

    @pytest.mark.asyncio
    async def test_parallel_delegate_executes_multiple(self, provider, mock_runner, mock_context):
        from src.agents.agent import AgentResult

        mock_runner.run = AsyncMock(return_value=AgentResult(
            response=AgentResponse(text="result", sender="test"),
        ))

        result = await provider.execute(
            "parallel_delegate",
            {
                "tasks": [
                    {"agent": "tool_calc", "objective": "compute", "task": "1+1"},
                    {"agent": "tool_terminal", "objective": "check", "task": "ls"},
                ]
            },
            context=mock_context,
        )
        assert "[tool_calc]" in result
        assert "[tool_terminal]" in result
        assert "result" in result

    @pytest.mark.asyncio
    async def test_parallel_delegate_empty_tasks(self, provider, mock_context):
        result = await provider.execute(
            "parallel_delegate",
            {"tasks": []},
            context=mock_context,
        )
        assert "错误" in result
