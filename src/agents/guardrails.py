"""Guardrail — Agent 输入/输出护栏。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class GuardrailResult:
    """护栏检查结果。"""

    passed: bool
    message: str = ""
    action: str = "block"  # "block" | "warn" | "rewrite"


@dataclass
class Guardrail:
    """输入/输出护栏。"""

    name: str
    check: Callable[..., Awaitable[GuardrailResult]]


async def run_guardrails(
    guardrails: list[Guardrail],
    context: Any,
    text: str,
) -> Optional[GuardrailResult]:
    """依次执行护栏列表，遇到 block 立即返回，全部通过返回 None。"""
    for guard in guardrails:
        result = await guard.check(context, text)
        if not result.passed and result.action == "block":
            return result
    return None
