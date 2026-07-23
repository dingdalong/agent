"""Moonshot LLM Provider — OpenAI 兼容接口，深度匹配 Kimi K3 特性。

Kimi K3（model id `k3(1M)`）与普通 OpenAI 兼容模型的关键差异：
- Preserved Thinking 恒开：推理始终开启且要求跨轮保留历史 `reasoning_content`，
  每条 assistant 消息（尤其带 tool_calls 的）必须原样回传 `reasoning_content`，
  否则报 400「reasoning_content is missing in assistant tool call message」。
- `reasoning_effort` 为顶层字段（low/high/max，默认 max），不使用 extra_body.thinking。
- 思考模型不可传 temperature；max_tokens 需足够大以容纳 reasoning + content。
- 支持上下文缓存，usage 走 OpenAI 口径 + prompt_tokens_details.cached_tokens。
"""

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

# Kimi 思考模型要求 max_tokens 足够大，以免 reasoning_content 与 content 被截断。
_MOONSHOT_MAX_TOKENS = 32768
_MOONSHOT_FINISH_REASONS = frozenset({"stop", "length", "tool_calls"})


class MoonshotProvider(LLMProvider):
    """Moonshot Provider (OpenAI 兼容 Chat Completions API)。"""

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

    # 有意不覆写基类的 clear_reasoning_content（无操作）：
    # K3 的 Preserved Thinking 恒开，要求跨轮保留历史 reasoning_content。
    # agent 在轮末调用 clear_reasoning_content 时保持无操作，reasoning_content
    # 便持续留在 history 并随每轮回传，精准匹配 K3 语义。切勿改成 DeepSeek 式剥离。

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """估算请求载荷 token 数（近似）。

        无内置 Kimi tokenizer，用字符数除以 4 的粗略估算，仅用于状态条、
        自动压缩与分页判定。

        Args:
            messages: 会话消息列表。
            prompt: 系统提示词消息列表，可为 None。
            tools: 工具 schema 列表，可为 None。

        Returns:
            估算的输入 token 数。
        """
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
        """把 Moonshot 的 usage 归一为统一 token 用量字典。

        Args:
            usage: 流式响应末块携带的 usage 对象，可为 None。

        Returns:
            统一键的 token 用量字典；usage 为 None 时返回 None。
        """
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

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        """归一化时回注 assistant 消息的 reasoning_content。

        normalize_messages 会重建消息、仅保留本钩子注入的额外字段。为满足 K3
        「带 tool_calls 的 assistant 消息必须携带 reasoning_content」的要求，
        当消息带 tool_calls 但缺 reasoning_content 时补空串，从源头避免 400。

        Args:
            msg: 原始消息字典。
            norm_msg: 归一化后的目标消息字典（就地写入）。
            role: 归一化后的角色。
        """
        if role != "assistant":
            return
        if msg.get("reasoning_content"):
            norm_msg["reasoning_content"] = msg["reasoning_content"]
        elif norm_msg.get("tool_calls"):
            norm_msg["reasoning_content"] = ""

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
        """向 Moonshot Chat Completions 发起一次流式调用。

        深度匹配 K3：不下发 temperature（思考模型不可修改），固定较大的
        max_tokens，思考通过顶层 reasoning_effort 控制（不使用 extra_body.thinking）。

        Args:
            messages: 会话消息列表。
            prompt: 系统提示词消息列表，可为 None。
            tools: 工具 schema 列表，可为 None。
            temperature: 采样温度（K3 忽略，不下发）。
            tool_choice: 工具选择策略。
            enable_thinking: 是否开启思考（K3 恒思考，关闭时仅不传 reasoning_effort）。
            reasoning_effort_override: 本次调用临时替换的推理力度档位；
                None 时沿用 provider 的 reasoning_effort。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLMResponse。
        """
        kwargs: dict = {
            "model": self.model,
            "messages": prompt + messages if prompt is not None else messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": _MOONSHOT_MAX_TOKENS,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if enable_thinking:
            kwargs["reasoning_effort"] = reasoning_effort_override or self.reasoning_effort

        response = await self._client.chat.completions.create(**kwargs)
        return await self._parse_stream(response, call=call)

    async def _parse_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """解析 Chat Completions 流式响应。

        Args:
            stream: OpenAI SDK 的异步流。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLMResponse，assistant_message 携带 reasoning_content。
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
                validate_chat_completion_stream(
                    finish_reason,
                    tool_calls,
                    valid_finish_reasons=_MOONSHOT_FINISH_REASONS,
                )

        validate_chat_completion_stream(
            finish_reason,
            tool_calls,
            valid_finish_reasons=_MOONSHOT_FINISH_REASONS,
        )
        content = "".join(content_parts) or ""
        reasoning_content = "".join(reasoning_parts) or ""
        if finish_reason != "length":
            call.mark_tool_fragments_complete()

        assistant_message: dict = {
            "role": "assistant",
            "content": content,
        }
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
        # K3 要求带 tool_calls 的 assistant 消息必须携带 reasoning_content，
        # 即使为空串；无 tool_calls 时仅在实际有思考内容时保留。
        if reasoning_content or tool_calls:
            assistant_message["reasoning_content"] = reasoning_content

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            token_usage=self._extract_token_usage(usage),
        )
