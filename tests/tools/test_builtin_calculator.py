"""测试内置 calculator 工具（通过新架构）。"""
import pytest
from src.tools.builtin.calculator import calculator


@pytest.mark.asyncio
async def test_calculator():
    result = await calculator(expression="2 + 2")
    assert "计算结果: 4" in result


@pytest.mark.asyncio
async def test_calculator_error():
    result = await calculator(expression="abc")
    assert "计算错误" in result


@pytest.mark.asyncio
async def test_calculator_complex():
    result = await calculator(expression="3 * 4 + 5")
    assert "计算结果: 17" in result
