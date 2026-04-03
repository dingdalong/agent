"""InputGuardrail — 输入安全检查，统一使用 Guardrail/GuardrailResult 体系。"""

from __future__ import annotations

import re

from src.guardrails.base import Guardrail, GuardrailResult

DEFAULT_PATTERNS = [
    r"忽略.*指令|忽略.*系统提示",
    r"删除.*文件|rm\s+-rf",
    r"DROP\s+TABLE",
    r"eval\s*\(",
    r"exec\s*\(",
]


def build_input_guardrails(
    patterns: list[str] | None = None,
) -> list[Guardrail]:
    """从 regex 模式列表构建输入护栏。

    每个 pattern 生成一个 Guardrail 实例，check 函数为异步，
    与 run_guardrails() 兼容。
    """
    patterns = patterns or DEFAULT_PATTERNS
    guardrails: list[Guardrail] = []
    for pattern in patterns:
        # 用默认参数捕获闭包变量，避免 late binding
        async def _check(_context, text: str, _pat: str = pattern) -> GuardrailResult:
            if re.search(_pat, text, re.IGNORECASE):
                return GuardrailResult(
                    passed=False,
                    message=f"输入包含不安全内容（匹配模式：{_pat}）",
                )
            return GuardrailResult(passed=True)

        guardrails.append(Guardrail(name=f"input_regex_{pattern[:20]}", check=_check))
    return guardrails
