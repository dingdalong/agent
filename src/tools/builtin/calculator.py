import ast
import math
import operator
import statistics
from typing import Callable

from src.tools import AccessKind, DataFlow, ToolPolicy
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

# 阶乘上限：超过即拒绝，避免 factorial(10**9) 之类占满线程 CPU/内存。
_FACTORIAL_LIMIT = 100000


def _safe_factorial(n: int) -> int:
    """带上限的阶乘，防止超大参数卡死。

    Args:
        n: 非负整数。
    Returns:
        n 的阶乘。
    """
    if n > _FACTORIAL_LIMIT:
        raise ValueError(f"factorial 参数过大（上限 {_FACTORIAL_LIMIT}）")
    return math.factorial(n)


# 白名单函数：名称 → 可调用对象，仅这些能在表达式中被调用（math/内置/statistics）。
# sum/mean/median 用变参 lambda 包一层，支持 sum(1, 2, 3) 这种平铺写法。
SAFE_FUNCTIONS: dict[str, Callable] = {
    # 幂与根
    "sqrt": math.sqrt,
    "cbrt": math.cbrt,
    "exp": math.exp,
    "pow": pow,
    # 对数
    "log": math.log,          # log(x) 自然对数；log(x, base) 指定底数
    "ln": math.log,
    "log2": math.log2,
    "log10": math.log10,
    # 三角与角度
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "degrees": math.degrees,
    "radians": math.radians,
    # 双曲
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    # 取整与符号
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "fabs": math.fabs,
    "copysign": math.copysign,
    # 整数与组合
    "factorial": _safe_factorial,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "comb": math.comb,
    "perm": math.perm,
    # 聚合
    "min": min,
    "max": max,
    "hypot": math.hypot,
    "sum": lambda *a: sum(a),
    "mean": lambda *a: statistics.fmean(a),
    "median": lambda *a: statistics.median(a),
}

# 白名单常量：名称 → 数值，仅这些裸名称可在表达式中出现。
SAFE_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}


def _safe_eval(node):
    """递归求值 AST 节点，只允许数字、白名单运算符/函数/常量。

    Args:
        node: 待求值的 AST 节点。
    Returns:
        求值结果（数值）。
    """
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
    elif isinstance(node, ast.Call):
        # 只放行 func 为白名单裸名称的调用；ast.Attribute（如 (1).__class__、math.__x__）
        # 因不是 ast.Name 被挡下，关键字/星号参数一律拒绝，__import__/open 等因不在白名单被拒。
        if not isinstance(node.func, ast.Name):
            raise ValueError("不支持的函数调用形式")
        fname = node.func.id
        if fname not in SAFE_FUNCTIONS:
            raise ValueError(f"不支持的函数: {fname}")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        args = [_safe_eval(a) for a in node.args]  # ast.Starred 会落到 else 被拒
        return SAFE_FUNCTIONS[fname](*args)
    elif isinstance(node, ast.Name):
        if node.id not in SAFE_NAMES:
            raise ValueError(f"未知的名称: {node.id}")
        return SAFE_NAMES[node.id]
    else:
        raise ValueError(f"不允许的表达式类型: {type(node).__name__}")


def safe_calc(expression: str):
    """安全计算数学表达式。

    Args:
        expression: 数学表达式字符串。
    Returns:
        计算结果（数值）。
    """
    tree = ast.parse(expression, mode="eval")
    return _safe_eval(tree)


class CalculatorInput(BaseModel):
    """计算数学表达式，支持算术、数学函数与常量，例如 'sqrt(3**2 + 4**2)'。"""
    expression: str = Field(description="要计算的数学表达式")


@tool(
    model=CalculatorInput,
    description="AST 安全求值数学表达式：支持算术运算（+ - * / // % **），数学函数"
                "（sqrt/cbrt/exp/log/ln/log2/log10、sin/cos/tan 及反三角、degrees/radians、"
                "floor/ceil/round/abs/trunc、factorial/gcd/lcm/comb/perm、min/max/sum/mean/median/hypot 等），"
                "以及常量 pi/e/tau/inf。可自由组合，如 'log(100, 10)'、'sin(pi/2)'、'factorial(6)'。",
    policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True),
)
def calculator(expression: str) -> str:
    """安全求值一段数学表达式。

    Args:
        expression: 要计算的数学表达式。
    Returns:
        以「计算结果: 」开头的结果字符串；出错时返回以「计算错误：」开头的说明。
    """
    try:
        result = safe_calc(expression)
        return f"计算结果: {result}"
    except Exception as e:
        return f"计算错误：{str(e)}"
