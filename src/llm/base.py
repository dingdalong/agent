"""LLM Provider 抽象基类与结构化输出支持。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import re
import asyncio
import math
import httpx
from src.events import EventBus
from src.tools import ToolDict

logger = logging.getLogger(__name__)

@dataclass
class LLMResponse:
    """LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    assistant_message: Optional[dict] = None

@dataclass
class LLMProvider(ABC):
    """所有 LLM 实现的抽象基类。"""
    api_key: str
    base_url: str
    model: str
    event_bus: EventBus
    concurrency: int = 5
    max_retries: int = 6
    timeout: float = 120.0
    context_limit: int = 0
    page_token_rate: float = 0.03
    page_token_budget: int = field(init=False)
    supports_native_structured_output: bool = False
    reasoning_effort: str = "max"
    preserve_thinking: bool = False

    _retryable_errors = ()

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.page_token_budget = max(1, math.floor(self.context_limit * self.page_token_rate))

    def clear_reasoning_content(self, message): ...

    @abstractmethod
    def estimate_tokens(self, message: list[dict]) -> int: ...

    def _split_page_once(self, text: str) -> tuple[str, str]:
        if self.estimate_tokens([{"role": "tool", "content": text}]) <= self.page_token_budget:
            return text, ""

        lo = 0
        hi = len(text)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.estimate_tokens([{"role": "tool", "content": text[:mid]}]) <= self.page_token_budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best <= 0:
            best = 1
        return text[:best], text[best:]

    def split_page(self, text: str) -> list[str]:
        pages: list[str] = []
        remaining = text
        while remaining:
            page, remaining = self._split_page_once(remaining)
            pages.append(page)
        return pages or [""]

    def micro_compact(self, messages: list[dict], keep_recent: int) -> list[dict]:
        tool_groups: list[tuple[int, list[dict]]] = []
        grouped_tool_indices: set[int] = set()

        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue

            call_ids = {
                call.get("id")
                for call in msg.get("tool_calls", [])
                if isinstance(call, dict) and call.get("id")
            }
            if not call_ids:
                continue

            group: list[dict] = []
            for j in range(i + 1, len(messages)):
                tool_msg = messages[j]
                if tool_msg.get("role") != "tool":
                    break
                if tool_msg.get("tool_call_id") not in call_ids:
                    break
                group.append(tool_msg)
                grouped_tool_indices.add(j)

            if group:
                tool_groups.append((i, group))

        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and i not in grouped_tool_indices:
                tool_groups.append((i, [msg]))

        if len(tool_groups) <= keep_recent:
            return messages
        tool_groups.sort(key=lambda group: group[0])
        for _, group in tool_groups[:-keep_recent]:
            for msg in group:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 120:
                    msg["content"] = "[Earlier tool result compacted. Re-run the tool if you need full detail.]"
        return messages

    @abstractmethod
    def normalize_messages(
        self,
        message: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False
    ) -> int: ...

    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        retryable = self._retryable_errors + (asyncio.TimeoutError, httpx.ReadTimeout)
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    return await self._do_chat(messages, prompt, tools, temperature, tool_choice)
                except retryable as e:
                    if attempt >= self.max_retries:
                        raise
                    wait_time = min(2 ** attempt * 5, 60)
                    logger.warning(f"API错误 ({type(e).__name__})，{wait_time}秒后重试 ({attempt+1}/{self.max_retries})...")
                    await asyncio.sleep(wait_time)
            raise RuntimeError("LLM chat: 所有重试均失败")

    @abstractmethod
    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse: ...
