"""结构化安全审查的 LLM 调用策略测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from src.mgr.data_guard import DataGuard
from src.mgr.permission_mgr import LLMJudgeClient
from src.mgr.web_safety_mgr import LLMWebSafetyClient


class RecordingProvider:
    """记录结构化审查传入的 provider 调用参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> SimpleNamespace:
        """记录参数并返回合法的结构化 allow 裁决。"""
        self.calls.append(kwargs)
        return SimpleNamespace(tool_calls={
            0: {
                "name": "record_verdict",
                "arguments": json.dumps({"decision": "allow", "reason": "ok"}),
            }
        })


class RecordingLLMMgr:
    """返回固定 provider 并记录请求的模型别名。"""

    def __init__(self, provider: RecordingProvider) -> None:
        self.provider = provider
        self.models: list[str] = []

    def get(self, model: str) -> RecordingProvider:
        """记录模型并返回测试 provider。"""
        self.models.append(model)
        return self.provider


def test_permission_judge_caps_structured_review_at_three_attempts() -> None:
    """智能权限应使用 fast 模型并把结构化审查限制为最多三次。"""
    provider = RecordingProvider()
    llm_mgr = RecordingLLMMgr(provider)
    client = LLMJudgeClient(llm_mgr, DataGuard())

    verdict = asyncio.run(client.judge({"tool": "shell"}))

    assert verdict.decision == "allow"
    assert llm_mgr.models == ["fast"]
    assert provider.calls[0]["max_attempts_cap"] == 3
    assert provider.calls[0]["tool_choice"] == "required"


def test_web_safety_caps_structured_review_at_three_attempts() -> None:
    """Web 安全审查应保留当前模型并限制为最多三次。"""
    provider = RecordingProvider()
    llm_mgr = RecordingLLMMgr(provider)
    client = LLMWebSafetyClient(llm_mgr, DataGuard())

    verdict = asyncio.run(client.review({"url": "https://example.test"}, model="current"))

    assert verdict.decision == "allow"
    assert llm_mgr.models == ["current"]
    assert provider.calls[0]["max_attempts_cap"] == 3
    assert provider.calls[0]["tool_choice"] == "required"
