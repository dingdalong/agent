"""Ollama LLM Provider — OpenAI 兼容接口。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterable
from openai import AsyncOpenAI
from src.llm.base import (
    LLMCallContext,
    LLMProvider,
    LLMResponse,
    iter_llm_stream,
    validate_chat_completion_stream,
)
from src.llm.errors import LLMStreamResponseError

if TYPE_CHECKING:
    from src.tools import ToolDict

logger = logging.getLogger(__name__)

_OLLAMA_FINISH_REASONS = frozenset({"stop", "length", "tool_calls"})


class OllamaProvider(LLMProvider):
    """Ollama Provider (OpenAI 兼容 Chat Completions API)"""

    _EFFORT_DOWNGRADE = {"max": "high", "xhigh": "high", "high": "medium", "medium": "low"}

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        if not self.base_url:
            self.base_url = "http://localhost:11434/v1"
        self._client = AsyncOpenAI(
            api_key=self.api_key or "ollama",
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
            default_headers=self._ua_headers(self.user_agent),
        )

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        all_messages = (prompt or []) + messages
        messages_for_estimate = [{
            "messages": all_messages,
            "tools": tools,
        }] if tools else all_messages
        return len(str(messages_for_estimate)) // 4

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        if usage is None:
            return None
        prompt_tokens_details = getattr(usage, "prompt_tokens_details", None)
        cache_creation_input_tokens = getattr(
            prompt_tokens_details,
            "cache_creation_tokens",
            None,
        )
        if cache_creation_input_tokens is None:
            cache_creation_input_tokens = getattr(
                prompt_tokens_details,
                "cache_creation_input_tokens",
                None,
            )
        return {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(prompt_tokens_details, "cached_tokens", None),
            "cache_creation_input_tokens": cache_creation_input_tokens,
        }

    def _normalize_role(self, role: str) -> str:
        if role == "developer":
            return "system"
        return role

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role != "assistant":
            return
        if msg.get("reasoning"):
            norm_msg["reasoning"] = msg["reasoning"]
        if msg.get("reasoning_content"):
            norm_msg["reasoning_content"] = msg["reasoning_content"]

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """向 Ollama 兼容接口发起一次流式调用。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词列表。
            tools: 可选工具 schema 列表。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位；
                None 时沿用 provider 的 reasoning_effort。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        kwargs: dict = {
            "model": self.model,
            "messages": prompt + messages if prompt else messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        if enable_thinking:
            effort = reasoning_effort_override or self.reasoning_effort
            if effort and effort.lower() != "none":
                kwargs["reasoning_effort"] = effort

            if self.preserve_thinking:
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {
                        "enable_thinking": True,
                        "preserve_thinking": True
                    }
                }
        else:
            kwargs["extra_body"] = {
                "chat_template_kwargs": {
                    "enable_thinking": False,
                }
            }

        response = await self._client.chat.completions.create(**kwargs)
        return await self._parse_stream(response, call=call)

    async def _parse_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """解析 Chat Completions 响应并即时记录工具片段。

        Args:
            stream: OpenAI 兼容异步响应流。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_content_parts: list[str] = []
        finish_reason = None
        usage = None

        async for chunk in iter_llm_stream(stream):
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            if not getattr(chunk, "choices", None):
                continue

            if finish_reason is not None:
                raise LLMStreamResponseError(
                    "业务终态后收到额外 choice",
                    code="invalid_response",
                )

            choice = chunk.choices[0]
            delta = choice.delta

            reasoning = getattr(delta, "reasoning", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                await self.emit_thinking_delta(reasoning, call=call)
            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content:
                reasoning_content_parts.append(reasoning_content)
                await self.emit_thinking_delta(reasoning_content, call=call)

            if delta.content:
                content_parts.append(delta.content)
                await self.emit_response_delta(delta.content, call=call)

            if delta.tool_calls:
                for tool_chunk in delta.tool_calls:
                    idx = tool_chunk.index
                    call_id = tool_chunk.id or ""
                    name = tool_chunk.function.name or ""
                    arguments = tool_chunk.function.arguments or ""
                    call.record_tool_fragment(
                        idx,
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if call_id:
                        tool_calls[idx]["id"] = call_id
                    tool_calls[idx]["name"] += name
                    tool_calls[idx]["arguments"] += arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason
                validate_chat_completion_stream(
                    finish_reason,
                    tool_calls,
                    valid_finish_reasons=_OLLAMA_FINISH_REASONS,
                )

        validate_chat_completion_stream(
            finish_reason,
            tool_calls,
            valid_finish_reasons=_OLLAMA_FINISH_REASONS,
        )
        raw_content = "".join(content_parts)
        content = raw_content.strip() if tool_calls else raw_content
        reasoning = "".join(reasoning_parts)
        reasoning_content = "".join(reasoning_content_parts)
        if finish_reason != "length":
            call.mark_tool_fragments_complete()

        assistant_message: dict = {
            "role": "assistant",
            "content": content or None,
        }
        if reasoning:
            assistant_message["reasoning"] = reasoning
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
            if not finish_reason:
                finish_reason = "tool_calls"

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            token_usage=self._extract_token_usage(usage),
        )
