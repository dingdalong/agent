from . import tool
from pydantic import BaseModel, Field
import asyncio

class AsyncCalculator(BaseModel):
    """计算数学表达式，例如 '2 + 3 * 4'，注意安全"""
    expression: str = Field(description="要计算的数学表达式")

@tool(name="calculator", description="计算数学表达式（生产环境请替换 eval）")
async def calculator(expression: str) -> str:
    await asyncio.sleep(0.1)  # 模拟异步操作
    try:
        result = eval(expression)   # 注意安全风险
        return f"计算结果: {result}"
    except Exception as e:
        return f"计算错误：{str(e)}"
