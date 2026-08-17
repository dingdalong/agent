"""LLM 风险审查共用的结构化裁决协议。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.mgr.data_guard import DataGuard
from src.tools import ToolDict


_STRUCTURED_REVIEW_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    decision: Literal["allow", "deny", "ask"]
    reason: str = ""


class ReviewClient(Protocol):
    async def review(self, request: Mapping[str, Any], **kwargs: Any) -> ReviewVerdict:
        """返回一次无缓存的结构化裁决。"""


RECORD_VERDICT_TOOL: ToolDict = {
    "type": "function",
    "function": {
        "name": "record_verdict",
        "description": "记录本次调用的风险裁决。",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["allow", "deny", "ask"]},
                "reason": {"type": "string", "maxLength": 500},
            },
            "required": ["decision", "reason"],
            "additionalProperties": False,
        },
    },
}


class StructuredVerdictRunner:
    """用指定 provider 执行统一的强制结构化裁决。"""

    def __init__(self, data_guard: DataGuard) -> None:
        self.data_guard = data_guard

    async def run(
        self,
        provider: Any,
        request: Mapping[str, Any],
        system_prompt: str,
    ) -> ReviewVerdict:
        response = await provider.chat(
            messages=[{"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
            prompt=[{"role": "system", "content": system_prompt}],
            tools=[RECORD_VERDICT_TOOL],
            # Kimi K3 thinking 恒开，"specified" 形式 tool_choice 会被服务端拒绝（400）；
            # 本调用只声明 record_verdict 一个工具，"required" 与指定函数语义等价。
            tool_choice="required",
            temperature=0.0,
            enable_thinking=False,
            reasoning_effort_override="low",
            max_attempts_cap=_STRUCTURED_REVIEW_MAX_ATTEMPTS,
        )
        for call in (getattr(response, "tool_calls", None) or {}).values():
            if call.get("name") != "record_verdict":
                continue
            try:
                payload = json.loads(call.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            decision = payload.get("decision")
            if decision in {"allow", "deny", "ask"}:
                reason = str(self.data_guard.redact(payload.get("reason") or ""))[:500]
                return ReviewVerdict(decision, reason)
        raise ValueError("审查模型未返回有效结构化裁决")
