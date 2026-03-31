"""DelegateToolProvider 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.tools.categories import CategoryResolver
from src.agents.registry import AgentRegistry
from src.agents.deps import AgentDeps


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
def mock_deps():
    from src.agents.deps import AgentDeps
    return AgentDeps()


@pytest.fixture
def provider(resolver, mock_runner, mock_registry, mock_deps):
    from src.tools.delegate import DelegateToolProvider
    return DelegateToolProvider(
        resolver=resolver, runner=mock_runner, registry=mock_registry, deps=mock_deps,
    )


def test_can_handle(provider):
    assert provider.can_handle("delegate_tool_terminal") is True
    assert provider.can_handle("delegate_tool_calc") is True
    assert provider.can_handle("delegate_tool_unknown") is False
    assert provider.can_handle("tool_terminal") is False
    assert provider.can_handle("calculate") is False


def test_get_schemas(provider):
    schemas = provider.get_schemas()
    assert len(schemas) == 2
    names = {s["function"]["name"] for s in schemas}
    assert names == {"delegate_tool_terminal", "delegate_tool_calc"}
    terminal_schema = next(s for s in schemas if s["function"]["name"] == "delegate_tool_terminal")
    assert terminal_schema["type"] == "function"
    params = terminal_schema["function"]["parameters"]
    assert "objective" in params["properties"]
    assert "task" in params["properties"]
    assert set(params["required"]) == {"objective", "task"}


@pytest.mark.asyncio
async def test_execute_delegates_to_runner(provider, mock_runner):
    from src.agents.agent import AgentResult
    mock_runner.run = AsyncMock(return_value=AgentResult(text="执行完成"))
    result = await provider.execute("delegate_tool_terminal", {"task": "列出当前目录"})
    assert result == "执行完成"
    mock_runner.run.assert_called_once()
    call_args = mock_runner.run.call_args
    agent = call_args[0][0]
    assert agent.name == "tool_terminal"
    ctx = call_args[0][1]
    assert ctx.input == "列出当前目录"


@pytest.mark.asyncio
async def test_execute_unknown_agent(provider):
    # Replace registry with one that can't resolve the agent
    provider._registry = MagicMock()
    provider._registry.get = MagicMock(return_value=None)
    result = await provider.execute("delegate_tool_unknown", {"task": "test"})
    assert "错误" in result


@pytest.mark.asyncio
async def test_execute_ensures_mcp_connection():
    """execute 应在运行 agent 前确保 MCP server 已连接。"""
    from src.tools.delegate import DelegateToolProvider
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
    test_runner.run = AsyncMock(return_value=AgentResult(text="done"))
    test_deps = AgentDeps()

    mock_mcp = AsyncMock()
    mock_mcp.ensure_servers_for_tools = AsyncMock()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
        mcp_manager=mock_mcp,
    )
    await provider.execute("delegate_tool_files", {"task": "read something"})
    mock_mcp.ensure_servers_for_tools.assert_called_once_with([
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
    ])


@pytest.mark.asyncio
async def test_execute_no_mcp_manager_still_works():
    """没有 mcp_manager 时（纯本地工具）execute 仍正常工作。"""
    from src.tools.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(text="42"))
    test_deps = AgentDeps()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
    )
    result = await provider.execute("delegate_tool_calc", {"task": "1+1"})
    assert result == "42"


@pytest.mark.asyncio
async def test_execute_propagates_delegate_depth():
    """execute 创建的子 RunContext 应将 delegate_depth 递增。"""
    from src.tools.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(text="done"))
    test_deps = AgentDeps()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
    )
    await provider.execute("delegate_tool_terminal", {"task": "run ls"})

    call_args = test_runner.run.call_args
    sub_ctx = call_args[0][1]
    assert sub_ctx.delegate_depth == 1


@pytest.mark.asyncio
async def test_execute_propagates_parent_delegate_depth():
    """当 delegate_depth 已非零时，子 RunContext 应继续递增。"""
    from src.tools.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(text="done"))
    test_deps = AgentDeps()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
    )
    provider.set_delegate_depth(1)
    await provider.execute("delegate_tool_terminal", {"task": "run ls"})

    call_args = test_runner.run.call_args
    sub_ctx = call_args[0][1]
    assert sub_ctx.delegate_depth == 2


def test_build_receiving_input_all_fields():
    """所有字段都有值时，接收方 input 包含全部信息。"""
    from src.tools.delegate import _build_receiving_input

    result = _build_receiving_input(
        objective="判断明天是否适合去故宫",
        task="查询北京明天天气",
        context="用户计划明天去故宫",
        expected_result="天气状况、温度、出行建议",
    )
    assert "最终目标：判断明天是否适合去故宫" in result
    assert "具体任务：查询北京明天天气" in result
    assert "相关上下文：用户计划明天去故宫" in result
    assert "期望结果：天气状况、温度、出行建议" in result
    assert "不要猜测或假设" in result
    assert "已完成 / 信息不足 / 失败" in result


def test_build_receiving_input_optional_fields_missing():
    """可选字段为空时，不显示对应行。"""
    from src.tools.delegate import _build_receiving_input

    result = _build_receiving_input(
        objective="帮用户查天气",
        task="查询天气预报",
        context=None,
        expected_result=None,
    )
    assert "最终目标：帮用户查天气" in result
    assert "具体任务：查询天气预报" in result
    assert "相关上下文" not in result
    assert "期望结果" not in result
    assert "不要猜测或假设" in result


def test_build_receiving_input_empty_string_treated_as_missing():
    """空字符串应与 None 同样处理，不显示对应行。"""
    from src.tools.delegate import _build_receiving_input

    result = _build_receiving_input(
        objective="目标",
        task="任务",
        context="",
        expected_result="",
    )
    assert "相关上下文" not in result
    assert "期望结果" not in result


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
