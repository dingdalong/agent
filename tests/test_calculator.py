"""calculator 工具的单元测试：确定性表达式断言精确值，浮点结果按容差断言，
非法/不安全表达式走错误路径返回字符串不抛异常。

工具函数经 @tool 装饰后仍是原函数（装饰器 return func），且不依赖注入的
context，故可直接 import 调用。"""

from __future__ import annotations

from src.tools.builtin.calculator import calculator, safe_calc


# ---------------------------------------------------------------------------
# 回归 —— 原有算术运算
# ---------------------------------------------------------------------------

def test_basic_arithmetic():
    assert calculator("2 + 3 * 4") == "计算结果: 14"
    assert safe_calc("(2 + 3) * 4") == 20
    assert safe_calc("17 % 5") == 2
    assert safe_calc("17 // 5") == 3
    assert safe_calc("-5 + 2") == -3
    assert safe_calc("2 ** 10") == 1024


# ---------------------------------------------------------------------------
# 数学函数 —— 整数/精确结果断言精确值
# ---------------------------------------------------------------------------

def test_functions_exact():
    assert safe_calc("sqrt(16)") == 4.0
    assert safe_calc("factorial(5)") == 120
    assert safe_calc("gcd(12, 18)") == 6
    assert safe_calc("lcm(4, 6)") == 12
    assert safe_calc("comb(5, 2)") == 10
    assert safe_calc("perm(5, 2)") == 20
    assert safe_calc("floor(3.7)") == 3
    assert safe_calc("ceil(3.2)") == 4
    assert safe_calc("round(3.14159, 2)") == 3.14
    assert safe_calc("abs(-7)") == 7
    assert safe_calc("log(100, 10)") == 2.0
    assert safe_calc("log2(8)") == 3.0


def test_functions_float_tolerance():
    assert abs(safe_calc("sin(pi / 2)") - 1.0) < 1e-9
    assert abs(safe_calc("cos(0)") - 1.0) < 1e-9
    assert abs(safe_calc("exp(0)") - 1.0) < 1e-9
    assert abs(safe_calc("degrees(pi)") - 180.0) < 1e-9


# ---------------------------------------------------------------------------
# 常量与复合表达式
# ---------------------------------------------------------------------------

def test_constants_and_composition():
    assert safe_calc("sqrt(3**2 + 4**2)") == 5.0
    assert abs(safe_calc("2 * pi") - 6.283185307179586) < 1e-9
    assert abs(safe_calc("e") - 2.718281828459045) < 1e-9


# ---------------------------------------------------------------------------
# 聚合函数 —— 变参平铺写法
# ---------------------------------------------------------------------------

def test_aggregates():
    assert safe_calc("sum(1, 2, 3)") == 6
    assert safe_calc("max(3, 7, 2)") == 7
    assert safe_calc("min(3, 7, 2)") == 2
    assert safe_calc("mean(2, 4)") == 3.0
    assert safe_calc("median(1, 3, 5)") == 3


def test_calculator_success_format():
    assert calculator("factorial(6)") == "计算结果: 720"


# ---------------------------------------------------------------------------
# 安全与错误路径 —— 返回错误字符串而非抛异常
# ---------------------------------------------------------------------------

def test_unknown_function_returns_error():
    assert calculator("foo(2)").startswith("计算错误：")


def test_unknown_name_returns_error():
    assert calculator("x + 1").startswith("计算错误：")


def test_attribute_access_rejected():
    # (1).__class__ 属性访问必须被挡下。
    assert calculator("(1).__class__").startswith("计算错误：")


def test_keyword_argument_rejected():
    assert calculator("round(1.5, ndigits=0)").startswith("计算错误：")


def test_dunder_import_rejected():
    assert calculator("__import__('os')").startswith("计算错误：")


def test_factorial_over_limit_returns_error():
    assert calculator("factorial(200000)").startswith("计算错误：")


def test_bad_syntax_returns_error():
    assert calculator("2 +").startswith("计算错误：")
