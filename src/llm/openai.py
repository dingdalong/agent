"""OpenAI SDK 实现的 LLM Provider。"""

import asyncio
import logging
import time

from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIError

from src.events.bus import EventBus
from src.events.types import TokenDelta, ThinkingDelta
from src.llm.types import LLMResponse

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """基于 OpenAI SDK 的 LLM Provider。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        concurrency: int = 5,
        max_retries: int = 3,
        timeout: float = 120.0,
        event_bus: EventBus | None = None,
    ):
        self.model = model
        self.max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._bus = event_bus
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=2,
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        silent: bool = False,
    ) -> LLMResponse:
        """流式调用 LLM，返回完整响应。"""
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        tools=tools,
                        stream=True,
                        temperature=temperature,
                        tool_choice=tool_choice or ("auto" if tools else None),
                    )
                    return await self._parse_stream(response, silent=silent)

                except (APIConnectionError, RateLimitError, asyncio.TimeoutError) as e:
                    if attempt == self.max_retries - 1:
                        raise
                    wait_time = 2 ** attempt
                    logger.warning(f"API错误 ({type(e).__name__})，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

                except APIError:
                    raise

    async def _parse_stream(
        self, stream, silent: bool = False,
    ) -> LLMResponse:
        """解析流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        finish_reason = None

        async for chunk in stream:
            delta = chunk.choices[0].delta

            # 思考内容（DeepSeek reasoning_content）
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning and not silent and self._bus:
                await self._bus.emit(ThinkingDelta(
                    timestamp=time.time(),
                    source=self.model,
                    content=reasoning,
                ))

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
                    content_parts.append(delta.content)
                    if not silent and self._bus:
                        await self._bus.emit(TokenDelta(
                            timestamp=time.time(),
                            source=self.model,
                            delta=delta.content,
                        ))

            if delta.tool_calls:
                for tool_chunk in delta.tool_calls:
                    idx = tool_chunk.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tool_chunk.id:
                        tool_calls[idx]["id"] = tool_chunk.id
                    if tool_chunk.function.name:
                        tool_calls[idx]["name"] += tool_chunk.function.name
                    if tool_chunk.function.arguments:
                        tool_calls[idx]["arguments"] += tool_chunk.function.arguments

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

        content = "".join(content_parts)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )
