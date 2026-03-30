import re

import pytest
from src.mcp.manager import MCPManager


def test_make_tool_name_simple():
    mgr = MCPManager()
    assert mgr._make_tool_name("filesystem", "read_file") == "mcp_filesystem_read_file"


def test_make_tool_name_hyphen_to_underscore():
    mgr = MCPManager()
    assert mgr._make_tool_name("my-remote-api", "query") == "mcp_my_remote_api_query"


def test_make_tool_name_special_chars():
    """Dots, spaces, and other special chars are sanitized to underscores."""
    mgr = MCPManager()
    assert mgr._make_tool_name("my.server", "read file") == "mcp_my_server_read_file"
    assert mgr._make_tool_name("server@v2", "tool#1") == "mcp_server_v2_tool_1"


def test_convert_tool_schema():
    mgr = MCPManager()
    class MockTool:
        name = "read_file"
        description = "Read a file from the filesystem"
        inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        annotations = None

    schema = mgr._convert_tool_schema("filesystem", MockTool())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp_filesystem_read_file"
    assert schema["function"]["description"] == "[filesystem] Read a file from the filesystem"
    assert schema["function"]["parameters"] == MockTool.inputSchema


def test_convert_result_text():
    mgr = MCPManager()
    class MockTextContent:
        type = "text"
        text = "file contents here"
    class MockResult:
        isError = False
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert result == "file contents here"


def test_convert_result_error():
    mgr = MCPManager()
    class MockTextContent:
        type = "text"
        text = "File not found"
    class MockResult:
        isError = True
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert "工具执行出错" in result
    assert "File not found" in result


def test_convert_result_truncation():
    mgr = MCPManager()
    class MockTextContent:
        type = "text"
        text = "x" * 3000
    class MockResult:
        isError = False
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert result.endswith("...(结果已截断)")
    assert len(result) == 2000 + len("...(结果已截断)")


def test_convert_result_empty():
    """Empty content returns a meaningful message instead of empty string."""
    mgr = MCPManager()
    class MockResult:
        isError = False
        content = []
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert result == "(执行成功，无输出)"


from unittest.mock import AsyncMock
from src.mcp.provider import MCPToolProvider


@pytest.mark.asyncio
async def test_mcp_provider_routes_mcp_tools():
    """MCPToolProvider routes mcp_-prefixed tools to mcp_manager."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = "mcp result"

    provider = MCPToolProvider(mock_mcp)
    assert provider.can_handle("mcp_filesystem_read_file")
    result = await provider.execute("mcp_filesystem_read_file", {"path": "/tmp/test"})

    mock_mcp.call_tool.assert_called_once_with("mcp_filesystem_read_file", {"path": "/tmp/test"})
    assert result == "mcp result"


def test_mcp_provider_does_not_handle_local_tools():
    """Non-mcp_ tools are not handled by MCPToolProvider."""
    mock_mcp = AsyncMock()
    provider = MCPToolProvider(mock_mcp)
    assert not provider.can_handle("calculator")
    assert not provider.can_handle("write_file")


from src.mcp.config import MCPServerConfig


def test_init_with_configs_stores_by_safe_name():
    """构造时传入 configs，按 safe_name 存储，不连接。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
        MCPServerConfig(name="my.api", transport="http", url="http://localhost:8080"),
    ]
    mgr = MCPManager(configs=configs)
    assert "desktop_commander" in mgr._configs
    assert "my_api" in mgr._configs
    assert mgr._sessions == {}  # 未连接


def test_init_without_configs():
    """无参构造仍然可用。"""
    mgr = MCPManager()
    assert mgr._configs == {}
    assert mgr._sessions == {}


@pytest.mark.asyncio
async def test_connect_server_idempotent():
    """已连接的 server 再次调用 connect_server 不会重复连接。"""
    configs = [
        MCPServerConfig(name="test-server", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    # 手动注入一个 fake session 来模拟已连接状态
    mgr._sessions["test_server"] = "fake_session"
    await mgr.connect_server("test_server")
    # session 不变，说明没有重新连接
    assert mgr._sessions["test_server"] == "fake_session"


def test_connect_server_unknown_name_raises():
    """传入未知 server name 应报错。"""
    mgr = MCPManager()
    import asyncio
    with pytest.raises(KeyError):
        asyncio.get_event_loop().run_until_complete(mgr.connect_server("nonexistent"))


# ---------------------------------------------------------------------------
# Task 2: ensure_servers_for_tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_servers_for_tools_connects_needed():
    """ensure_servers_for_tools 根据工具名前缀，连接所需但未连接的 server。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
        MCPServerConfig(name="another-server", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        if safe_name not in mgr._configs:
            raise KeyError(safe_name)
        mgr._sessions[safe_name] = "fake"
        connected.append(safe_name)

    mgr.connect_server = fake_connect

    await mgr.ensure_servers_for_tools([
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
        "calculator",
    ])
    assert connected == ["desktop_commander"]
    assert "another_server" not in connected


@pytest.mark.asyncio
async def test_ensure_servers_for_tools_skips_connected():
    """已连接的 server 不会重复连接。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
    ]
    mgr = MCPManager(configs=configs)
    mgr._sessions["desktop_commander"] = "already_connected"

    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        connected.append(safe_name)

    mgr.connect_server = fake_connect

    await mgr.ensure_servers_for_tools(["mcp_desktop_commander_read_file"])
    assert connected == []


@pytest.mark.asyncio
async def test_ensure_servers_for_tools_longest_prefix_match():
    """当存在 server name 前缀包含关系时，使用最长前缀匹配。"""
    configs = [
        MCPServerConfig(name="foo", transport="stdio", command="echo"),
        MCPServerConfig(name="foo-bar", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        if safe_name not in mgr._configs:
            raise KeyError(safe_name)
        mgr._sessions[safe_name] = "fake"
        connected.append(safe_name)

    mgr.connect_server = fake_connect

    await mgr.ensure_servers_for_tools(["mcp_foo_bar_some_tool"])
    assert connected == ["foo_bar"]


# ---------------------------------------------------------------------------
# Task 3: connect_all uses stored configs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_all_uses_stored_configs():
    """connect_all 不再需要 configs 参数，使用构造时存储的配置。"""
    configs = [
        MCPServerConfig(name="server-a", transport="stdio", command="echo"),
        MCPServerConfig(name="server-b", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    connected: list[str] = []

    async def fake_connect_one(config: MCPServerConfig) -> None:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", config.name)
        mgr._sessions[safe] = "fake"
        connected.append(config.name)

    mgr._connect_one = fake_connect_one

    await mgr.connect_all()
    assert set(connected) == {"server-a", "server-b"}
