"""OutputGuardrail — 输出安全检查。"""

from __future__ import annotations

from typing import Tuple


class OutputGuardrail:
    """输出安全检查。

    TODO: 尚未接入生产代码，当前仅有测试覆盖。
    接入时需统一返回类型为 GuardrailResult（与 run_guardrails 体系对齐）。
    """

    def __init__(self, blocked_content: list[str] | None = None):
        self.blocked_content = blocked_content or [
            "rm -rf",
            "DROP TABLE",
            "eval(",
        ]

    def check(self, output: str) -> Tuple[bool, str]:
        for phrase in self.blocked_content:
            if phrase in output:
                return False, f"输出包含不安全内容：{phrase}"
        return True, ""
