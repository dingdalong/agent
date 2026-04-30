"""LLM Provider 抽象基类与结构化输出支持。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import re
import asyncio
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
    supports_native_structured_output: bool = False
    reasoning_effort: str = "max"

    _retryable_errors = ()

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def clear_reasoning_content(self, message): ...

    @abstractmethod
    def estimate_tokens(self, message: list[dict]) -> int: ...

    @abstractmethod
    def micro_compact(self, messages: list[dict], keep_recent: int) -> list[dict]: ...

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
