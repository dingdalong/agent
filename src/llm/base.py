"""LLM Provider 抽象基类与结构化输出支持。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import asyncio
import math
import time
import random
import httpx
import openai
import anthropic
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

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.page_token_budget = max(1, math.floor(self.context_limit * self.page_token_rate))

    def clear_reasoning_content(self, message): ...

    @classmethod
    async def list_models(cls, api_key: str, base_url: str, timeout: float = 3.0) -> list[str]:
        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=3.0, max_retries=0)
        try:
            response = await asyncio.wait_for(client.models.list(), timeout=timeout)
            return [m.id for m in response.data]
        finally:
            await client.close()

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

    def _exception_status_code(self, exc: BaseException) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        return response_status if isinstance(response_status, int) else None

    def _exception_text(self, exc: BaseException) -> str:
        parts = [str(exc)]
        body = getattr(exc, "body", None)
        if body is not None:
            try:
                parts.append(json.dumps(body, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(body))
        response = getattr(exc, "response", None)
        response_text = getattr(response, "text", None)
        if response_text:
            parts.append(str(response_text))
        return "\n".join(part for part in parts if part).lower()

    def is_context_too_long_error(self, exc: BaseException) -> bool:
        text = self._exception_text(exc)
        patterns = (
            "context length",
            "maximum context",
            "prompt too long",
            "overlong_prompt",
            "input is too long",
            "tokens exceed",
            "too many tokens",
        )
        return any(pattern in text for pattern in patterns)

    def is_retryable_error(self, exc: BaseException) -> bool:
        if self.is_context_too_long_error(exc):
            return False

        non_retryable_types = (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,
            openai.BadRequestError,
            openai.UnprocessableEntityError,
            openai.APIResponseValidationError,
            openai.ContentFilterFinishReasonError,
            openai.LengthFinishReasonError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
            anthropic.UnprocessableEntityError,
            anthropic.APIResponseValidationError,
        )
        if isinstance(exc, non_retryable_types):
            return False

        retryable_types = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
            openai.ConflictError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.ConflictError,
            httpx.TimeoutException,
            httpx.TransportError,
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionError,
        )
        if isinstance(exc, retryable_types):
            return True

        status_code = self._exception_status_code(exc)
        if status_code in {408, 409, 429}:
            return True
        if status_code is not None and status_code >= 500:
            return True

        if isinstance(exc, OSError) and not isinstance(
            exc,
            (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError),
        ):
            return True

        return False

    async def _sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    def _retry_jitter(self) -> float:
        return random.uniform(0, 1)

    def _retry_delay(self, attempt: int) -> float:
        return min(2 ** attempt * 5, 60) + self._retry_jitter()

    def normalize_messages(
        self,
        messages: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False,
    ) -> list[dict]:
        VALID_ROLES = {"system", "user", "assistant", "tool"}
        if allow_developer_role:
            VALID_ROLES.add("developer")

        if isinstance(messages, dict):
            raw_messages = [messages]
        elif isinstance(messages, list):
            raw_messages = list(messages)
        else:
            raise TypeError(
                f"messages 必须是 dict 或 list[dict]，当前类型: {type(messages).__name__}"
            )

        normalized: list[dict] = []

        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                if strict:
                    raise TypeError(f"messages[{idx}] 必须是 dict，当前类型: {type(msg).__name__}")
                continue

            role = msg.get("role", "").strip().lower()
            if not role:
                if strict:
                    raise ValueError(f"messages[{idx}] 缺少必填字段 'role'")
                role = "user"

            role = self._normalize_role(role)

            if role not in VALID_ROLES:
                if strict:
                    raise ValueError(
                        f"messages[{idx}] role='{role}' 不被支持。"
                        f"支持的 role: {sorted(VALID_ROLES)}"
                    )
                role = "user"

            content = self._normalize_content(msg.get("content", ""))
            has_tool_calls = bool(msg.get("tool_calls"))

            if not content and not has_tool_calls and role != "tool":
                continue

            norm_msg: dict = {"role": role, "content": content}

            if role == "assistant" and has_tool_calls and allow_tool_calls:
                tool_calls = self._normalize_tool_calls(msg.get("tool_calls"))
                if tool_calls:
                    norm_msg["tool_calls"] = tool_calls

            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                if not tool_call_id and strict:
                    raise ValueError(f"messages[{idx}] role='tool' 但缺少 tool_call_id")
                norm_msg["tool_call_id"] = tool_call_id
                if not allow_tool_calls:
                    norm_msg["role"] = "user"
                    norm_msg.pop("tool_call_id", None)

            if "name" in msg and isinstance(msg["name"], str):
                norm_msg["name"] = msg["name"]

            self._normalize_assistant_extra(msg, norm_msg, role)

            normalized.append(norm_msg)

        return normalized

    def _normalize_role(self, role: str) -> str:
        return role

    def _normalize_content(self, content) -> str | list[dict] | None:
        if isinstance(content, list):
            return [p for p in content if isinstance(p, dict)] or ""
        if content is not None and not isinstance(content, str):
            return str(content)
        return content

    def _normalize_tool_calls(self, tool_calls) -> list[dict] | None:
        if not tool_calls or not isinstance(tool_calls, list):
            return None
        valid_calls = []
        for call in tool_calls:
            if isinstance(call, dict) and "function" in call:
                valid_calls.append({
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": call["function"],
                })
        return valid_calls or None

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        pass

    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        enable_thinking: bool = True,
    ) -> LLMResponse:
        async with self._semaphore:
            max_attempts = max(1, self.max_retries)
            for attempt in range(max_attempts):
                try:
                    started_at = await self._emit_llm_call_started(messages, prompt, tools, caller_agent_type, caller_uuid)
                    response = await self._do_chat(
                        messages,
                        prompt,
                        tools,
                        temperature,
                        tool_choice,
                        caller_agent_type,
                        caller_uuid,
                        enable_thinking,
                    )
                    await self._emit_llm_call_completed(
                        started_at=started_at,
                        usage=response.token_usage,
                        caller_uuid=caller_uuid,
                    )
                    return response
                except Exception as e:
                    if not self.is_retryable_error(e) or attempt >= max_attempts - 1:
                        raise
                    wait_time = self._retry_delay(attempt)
                    logger.warning(
                        "API错误 (%s)，%.1f秒后重试 (%d/%d)...",
                        type(e).__name__,
                        wait_time,
                        attempt + 1,
                        max_attempts,
                    )
                    await self._sleep(wait_time)
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
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> float:
        """发出 LLMCallStarted 事件并返回起始时间戳。

        Args:
            messages: 本次提交的消息列表。
            prompt: 系统提示词消息列表（可为 None）。
            tools: 本次可用工具列表（可为 None）。
            caller_agent_type: 发起本次调用的 agent 类型（主 Agent 为 None），透传给事件供 UI 活动行显示当前 agent。
            caller_uuid: 发起本次调用的 agent 实例 uuid。
        Returns:
            本次调用的起始时间戳（time.time()），供完成事件计算耗时。
        """
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
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))
        return started_at

    async def _emit_llm_call_completed(
        self,
        started_at: float | None,
        ended_at: float | None = None,
        usage: dict[str, int | None] | None = None,
        caller_uuid: str | None = None,
    ) -> None:
        """发出 LLMCallCompleted 事件。

        Args:
            started_at: 调用起始时间戳。
            ended_at: 调用结束时间戳（None 时用当前时间）。
            usage: token 用量字典。
            caller_uuid: 发起本次调用的 agent 实例 uuid，供路由器按 agent 累计 token。
        """
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
            caller_uuid=caller_uuid,
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
        enable_thinking: bool = True,
    ) -> LLMResponse: ...
