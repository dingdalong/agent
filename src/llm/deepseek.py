"""DeepSeek LLM Provider。"""

from __future__ import annotations

import logging
from functools import cached_property
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
import transformers

if TYPE_CHECKING:
    from src.tools import ToolDict

logger = logging.getLogger(__name__)

_DEEPSEEK_FINISH_REASONS = frozenset({"stop", "length", "tool_calls"})

class DeepSeekProvider(LLMProvider):
    """基于 OpenAI SDK 的 LLM Provider。"""

    _EFFORT_DOWNGRADE = {"max": "high"}

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
            default_headers=self._ua_headers(self.user_agent),
        )

    def clear_reasoning_content(self, messages):
        """清理思考内容"""
        for message in messages:
            # 处理对象（有 reasoning_content 属性）
            if hasattr(message, 'reasoning_content'):
                message.reasoning_content = None
            # 处理字典（有 'reasoning_content' 键）
            elif isinstance(message, dict) and 'reasoning_content' in message:
                message['reasoning_content'] = None

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
        return len(self._tokenizer.encode(str(messages_for_estimate)))

    @cached_property
    def _tokenizer(self):
        return transformers.AutoTokenizer.from_pretrained(
            "src/llm/tokenizer/deepseek", trust_remote_code=True
        )

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        if usage is None:
            return None
        return {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
        }

    def _normalize_content(self, content) -> str | list[dict]:
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(part)
                elif isinstance(part, str):
                    parts.append({"type": "text", "text": part})
            return parts if parts else ""
        if isinstance(content, str):
            return content.strip()
        if content is not None:
            return str(content)
        return ""

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role != "assistant":
            return
        if msg.get("prefix") is True:
            norm_msg["prefix"] = True
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
        """向 DeepSeek 兼容接口发起一次流式调用。

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
            "messages": prompt + messages if prompt is not None else messages,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
            "tool_choice": tool_choice or ("auto" if tools else None),
        }
        if enable_thinking:
            kwargs["reasoning_effort"] = reasoning_effort_override or self.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = await self._client.chat.completions.create(**kwargs)
        return await self._parse_stream(
            response,
            call=call,
        )

    async def _parse_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """解析流式响应并即时记录工具片段。

        Args:
            stream: OpenAI 兼容异步响应流。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
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

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is not None:
                reasoning_parts.append(reasoning)
                await self.emit_thinking_delta(reasoning, call=call)

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
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
                if finish_reason == "insufficient_system_resource":
                    raise LLMStreamResponseError(
                        "推理系统资源不足导致响应中断",
                        code="insufficient_system_resource",
                    )
                validate_chat_completion_stream(
                    finish_reason,
                    tool_calls,
                    valid_finish_reasons=_DEEPSEEK_FINISH_REASONS,
                )

        validate_chat_completion_stream(
            finish_reason,
            tool_calls,
            valid_finish_reasons=_DEEPSEEK_FINISH_REASONS,
        )
        content = "".join(content_parts) or ""
        reasoning_content = "".join(reasoning_parts) or ""
        if finish_reason != "length":
            call.mark_tool_fragments_complete()

        # 构造完整的 assistant 消息，包含 Provider 特有字段
        assistant_message: dict = {
            "role": "assistant",
            "content": content,
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
            token_usage=self._extract_token_usage(usage),
        )
