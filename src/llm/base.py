"""LLM Provider 抽象基类与结构化输出支持。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import re
import asyncio
import math
import time
import httpx
from src.events import EventBus
from src.events.types import LLMCallCompleted, LLMCallStarted, ResponseDelta, ThinkingDelta
from src.tools import ToolDict

logger = logging.getLogger(__name__)

@dataclass
class LLMResponse:
    """LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    assistant_message: Optional[dict] = None
    token_usage: dict[str, int | None] | None = None

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
    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int: ...

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
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        retryable = self._retryable_errors + (asyncio.TimeoutError, httpx.ReadTimeout)
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    started_at = await self._emit_llm_call_started(messages, prompt, tools)
                    response = await self._do_chat(
                        messages,
                        prompt,
                        tools,
                        temperature,
                        tool_choice,
                        caller_agent_type,
                        caller_uuid,
                    )
                    await self._emit_llm_call_completed(
                        started_at=started_at,
                        usage=response.token_usage,
                    )
                    return response
                except retryable as e:
                    if attempt >= self.max_retries - 1:
                        raise
                    wait_time = min(2 ** attempt * 5, 60)
                    logger.warning(f"API错误 ({type(e).__name__})，{wait_time}秒后重试 ({attempt+1}/{self.max_retries})...")
                    await asyncio.sleep(wait_time)
            raise RuntimeError("LLM chat: 所有重试均失败")

    async def emit_response_delta(
        self,
        content: str,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.emit(ResponseDelta(
            timestamp=time.time(),
            source=self.model,
            content=content,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def emit_thinking_delta(
        self,
        content: str,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.emit(ThinkingDelta(
            timestamp=time.time(),
            source=self.model,
            content=content,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def _emit_llm_call_started(
        self,
        messages: list[dict],
        prompt: list[dict] | None,
        tools: list[ToolDict] | None,
    ) -> float:
        started_at = time.time()
        if self.event_bus is None:
            return started_at

        all_messages = (prompt or []) + messages
        await self.event_bus.emit(LLMCallStarted(
            timestamp=started_at,
            source=self.model,
            model=self.model,
            estimated_input_tokens=self.estimate_tokens(messages, prompt, tools),
            message_count=len(all_messages),
            tool_count=len(tools or []),
        ))
        return started_at

    async def _emit_llm_call_completed(
        self,
        started_at: float | None,
        ended_at: float | None = None,
        usage: dict[str, int | None] | None = None,
    ) -> None:
        if self.event_bus is None or started_at is None:
            return

        completed_at = ended_at if ended_at is not None else time.time()
        duration = max(completed_at - started_at, 0.0)
        usage = usage or {}
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")

        await self.event_bus.emit(LLMCallCompleted(
            timestamp=completed_at,
            source=self.model,
            model=self.model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_input_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
            duration_seconds=duration,
            output_tokens_per_second=output_tokens / duration if output_tokens is not None and duration > 0 else None,
            total_tokens_per_second=total_tokens / duration if total_tokens is not None and duration > 0 else None,
        ))

    @abstractmethod
    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse: ...
