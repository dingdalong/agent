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
