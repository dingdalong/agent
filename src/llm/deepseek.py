"""DeepSeek LLM Provider。"""

import asyncio
import logging
import time
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIError
from src.events.types import ResponseDelta, ThinkingDelta
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict

logger = logging.getLogger(__name__)


class DeepSeekProvider(LLMProvider):
    """基于 OpenAI SDK 的 LLM Provider。"""

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        output_schema=None,
    ) -> LLMResponse:
        """流式调用 LLM，返回完整响应。"""
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        messages=prompt + messages if prompt is not None else messages,
                        tools=tools,
                        stream=True,
                        temperature=temperature,
                        tool_choice=tool_choice or ("auto" if tools else None),
                    )
                    return await self._parse_stream(response)

                except (APIConnectionError, RateLimitError, asyncio.TimeoutError) as e:
                    if attempt == self.max_retries - 1:
                        raise
                    wait_time = 2 ** attempt
                    logger.warning(f"API错误 ({type(e).__name__})，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

                except APIError:
                    raise
            raise RuntimeError("LLM chat: 所有重试均失败")

    async def _parse_stream(
        self, stream
    ) -> LLMResponse:
        """解析流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None

        async for chunk in stream:
            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                await self.event_bus.emit(ThinkingDelta(
                    timestamp=time.time(),
                    source=self.model,
                    content=reasoning,
                ))

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
                    content_parts.append(delta.content)
                    await self.event_bus.emit(ResponseDelta(
                        timestamp=time.time(),
                        source=self.model,
                        content=delta.content,
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
        reasoning_content = "".join(reasoning_parts) or None

        # 构造完整的 assistant 消息，包含 Provider 特有字段
        assistant_message: dict = {
            "role": "assistant",
            "content": content or None,
        }
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls.values()
            ]

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
        )
