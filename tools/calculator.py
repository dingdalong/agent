from pydantic import BaseModel, Field

class Calculator(BaseModel):
    """计算数学表达式，例如 '2 + 3 * 4'，注意安全"""
    expression: str = Field(description="要计算的数学表达式")

def calculator(expression: str) -> str:
    """计算数学表达式（生产环境请替换 eval）"""
    try:
        result = eval(expression)   # 注意安全风险
        return str(result)
    except Exception as e:
        return f"计算错误：{str(e)}"

# 添加别名
ToolModel = Calculator
execute = calculator