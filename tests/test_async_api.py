import pytest
import asyncio
from src.core.async_api import call_model

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