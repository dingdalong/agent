import pytest
import asyncio
from src.core.async_api import call_model, parse_stream_response
from src.core.performance import async_time_function


@pytest.mark.asyncio
async def test_call_model_mocked(mocker):
    """测试异步调用结构（使用模拟，统一流式）"""
    # 创建模拟流式响应
    class MockDelta:
        content = "Hello, world!"
        tool_calls = None

    class MockChoice:
        delta = MockDelta()
        finish_reason = "stop"

    class MockChunk:
        choices = [MockChoice()]

    async def mock_stream():
        yield MockChunk()

    async def mock_create(*args, **kwargs):
        assert kwargs.get("stream") is True, "call_model 应该始终使用流式调用"
        return mock_stream()

    mocker.patch('config.async_client.chat.completions.create', side_effect=mock_create)
    messages = [{"role": "user", "content": "Hello"}]

    content, tool_calls, finish_reason = await call_model(messages)
    assert content == "Hello, world!"
    assert tool_calls == {}
    assert finish_reason == "stop"


@pytest.mark.asyncio
async def test_parse_stream_response():
    """测试流式响应解析"""
    class MockDelta1:
        content = "Hello"
        tool_calls = None

    class MockChoice1:
        delta = MockDelta1()
        finish_reason = None

    class MockChunk1:
        choices = [MockChoice1()]

    class MockDelta2:
        content = " World"
        tool_calls = None

    class MockChoice2:
        delta = MockDelta2()
        finish_reason = "stop"

    class MockChunk2:
        choices = [MockChoice2()]

    async def mock_stream():
        yield MockChunk1()
        yield MockChunk2()

    content, tool_calls, finish_reason = await parse_stream_response(mock_stream())
    assert content == "Hello World"
    assert tool_calls == {}
    assert finish_reason == "stop"


@pytest.mark.asyncio
async def test_async_time_decorator(capsys):
    """测试异步计时装饰器"""

    @async_time_function()
    async def test_func():
        await asyncio.sleep(0.1)
        return "done"

    result = await test_func()
    assert result == "done"

    captured = capsys.readouterr()
    assert "test_func 耗时:" in captured.out
