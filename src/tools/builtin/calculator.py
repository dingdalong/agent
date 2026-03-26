import ast
import operator
import asyncio
from src.tools.decorator import tool
from pydantic import BaseModel, Field

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def _safe_eval(node):
    """递归求值 AST 节点，只允许数字和基本算术运算"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    elif isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in SAFE_OPERATORS:
            raise ValueError(f"不支持的运算符: {op_type.__name__}")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        return SAFE_OPERATORS[op_type](left, right)
    elif isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in SAFE_OPERATORS:
            raise ValueError(f"不支持的运算符: {op_type.__name__}")
        return SAFE_OPERATORS[op_type](_safe_eval(node.operand))
    else:
        raise ValueError(f"不允许的表达式类型: {type(node).__name__}")

def safe_calc(expression: str):
    """安全计算数学表达式"""
    tree = ast.parse(expression, mode="eval")
    return _safe_eval(tree)

class AsyncCalculator(BaseModel):
    """计算数学表达式，例如 '2 + 3 * 4'"""
    expression: str = Field(description="要计算的数学表达式")

@tool(model=AsyncCalculator, description="安全计算数学表达式")
async def calculator(expression: str) -> str:
    await asyncio.sleep(0.1)
    try:
        result = safe_calc(expression)
        return f"计算结果: {result}"
    except Exception as e:
        return f"计算错误：{str(e)}"
