from pydantic import BaseModel, Field
import asyncio

class AsyncCalculator(BaseModel):
    """异步计算数学表达式，例如 '2 + 3 * 4'，注意安全"""
    expression: str = Field(description="要计算的数学表达式")

async def async_calculator(expression: str) -> str:
    """异步计算数学表达式（生产环境请替换 eval）"""
    await asyncio.sleep(0.1)  # 模拟异步操作
    try:
        result = eval(expression)   # 注意安全风险
        return f"异步计算结果: {result}"
    except Exception as e:
        return f"异步计算错误：{str(e)}"

# 添加别名
ToolModel = AsyncCalculator
execute = async_calculator

# 可选：工具名称和描述
TOOL_NAME = "async_calculator"
TOOL_DESCRIPTION = "异步计算数学表达式"