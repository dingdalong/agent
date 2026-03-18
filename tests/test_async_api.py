import pytest
import asyncio
from src.core.async_api import call_model

@pytest.mark.asyncio
async def test_call_model_not_implemented():
    """测试骨架函数抛出未实现错误"""
    messages = [{"role": "user", "content": "Hello"}]
    with pytest.raises(NotImplementedError):
        await call_model(messages)