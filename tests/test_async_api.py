import pytest
import asyncio
from src.core.async_api import call_model, parse_nonstream_response, execute_tool_calls, _execute_single_tool

@pytest.mark.asyncio
async def test_call_model_not_implemented():
    """测试骨架函数抛出未实现错误"""
    messages = [{"role": "user", "content": "Hello"}]
    with pytest.raises(NotImplementedError):
        await call_model(messages)

@pytest.mark.asyncio
async def test_call_model_mocked(mocker):
    """测试异步调用结构（使用模拟）"""
    # 创建一个简单的模拟响应对象
    class MockResponse:
        choices = []

    # 模拟异步调用返回一个简单的响应对象
    async def mock_create(*args, **kwargs):
        return MockResponse()

    mocker.patch('config.async_client.chat.completions.create', side_effect=mock_create)
    messages = [{"role": "user", "content": "Hello"}]

    # 应该调用但抛出未实现错误（解析函数）
    with pytest.raises(NotImplementedError):
        await call_model(messages, stream=False)

@pytest.mark.asyncio
async def test_parse_nonstream_response():
    """测试非流式响应解析"""
    # 创建模拟响应对象
    class MockMessage:
        content = "Hello"
        tool_calls = None

    class MockChoice:
        message = MockMessage()
        finish_reason = "stop"

    class MockResponse:
        choices = [MockChoice()]

    response = MockResponse()
    content, tool_calls, finish_reason = await parse_nonstream_response(response, False)

    assert content == "Hello"
    assert tool_calls == {}
    assert finish_reason == "stop"


@pytest.mark.asyncio
async def test_execute_tool_calls_no_tools():
    """测试无工具调用情况"""
    result = await execute_tool_calls("Hello", {}, {})
    assert result == []


@pytest.mark.asyncio
async def test_execute_single_tool_sync():
    """测试同步工具执行"""
    def sync_tool(x):
        return x * 2

    tool_call = {
        "id": "test-id",
        "name": "sync_tool",
        "arguments": '{"x": 5}'
    }

    result = await _execute_single_tool(tool_call, {"sync_tool": sync_tool})
    assert result == 10