import pytest
import asyncio
from tools.calculator import calculator

@pytest.mark.asyncio
async def test_calculator():
    """测试计算器"""
    result = await calculator("2 + 2")
    assert "计算结果: 4" in result

@pytest.mark.asyncio
async def test_calculator_error():
    """测试计算器错误处理"""
    result = await calculator("invalid expression")
    assert "计算错误" in result

@pytest.mark.asyncio
async def test_calculator_complex():
    """测试计算器复杂表达式"""
    result = await calculator("3 * 4 + 5")
    assert "计算结果: 17" in result

@pytest.mark.asyncio
async def test_calculator_with_delay():
    """测试计算器延迟"""
    import time
    start_time = time.time()
    result = await calculator("1 + 1")
    end_time = time.time()
    duration = end_time - start_time

    # 应该有至少0.1秒的延迟（模拟异步操作）
    assert duration >= 0.1, f"预期延迟至少0.1秒，实际: {duration:.3f}秒"
    assert "计算结果: 2" in result
