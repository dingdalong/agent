import pytest
from src.mcp.manager import MCPManager


def test_make_tool_name_simple():
    mgr = MCPManager()
    assert mgr._make_tool_name("filesystem", "read_file") == "mcp_filesystem_read_file"


def test_make_tool_name_hyphen_to_underscore():
    mgr = MCPManager()
    assert mgr._make_tool_name("my-remote-api", "query") == "mcp_my_remote_api_query"


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
