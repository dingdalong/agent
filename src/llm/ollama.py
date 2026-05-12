"""Ollama LLM Provider — OpenAI 兼容接口。"""

import logging
import time
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, InternalServerError
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Ollama Provider (OpenAI 兼容 Chat Completions API)"""

    _retryable_errors = (APIConnectionError, RateLimitError, InternalServerError)

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        if not self.base_url:
            self.base_url = "http://localhost:11434/v1"
        self._client = AsyncOpenAI(
            api_key=self.api_key or "ollama",
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
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

    def normalize_messages(
        self,
        messages: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False,
    ) -> list[dict]:
        VALID_ROLES = {"system", "user", "assistant", "tool"}

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
            if role == "developer":
                role = "system"
            if role not in VALID_ROLES:
                if strict:
                    raise ValueError(
                        f"messages[{idx}] role='{role}' 不被支持。"
                        f"支持的 role: {sorted(VALID_ROLES)}"
                    )
                role = "user"

            content = msg.get("content", "")
            if isinstance(content, list):
                content = [p for p in content if isinstance(p, dict)] or ""
            elif content is not None and not isinstance(content, str):
                content = str(content)

            has_tool_calls = bool(msg.get("tool_calls"))

            if not content and not has_tool_calls and role != "tool":
                continue

            norm_msg: dict = {"role": role, "content": content}

            if role == "assistant" and has_tool_calls and allow_tool_calls:
                tool_calls = msg.get("tool_calls", [])
                valid_calls = []
                for call in (tool_calls if isinstance(tool_calls, list) else []):
                    if isinstance(call, dict) and "function" in call:
                        valid_calls.append({
                            "id": call.get("id", ""),
                            "type": call.get("type", "function"),
                            "function": call["function"],
                        })
                if valid_calls:
                    norm_msg["tool_calls"] = valid_calls

            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                if not tool_call_id and strict:
                    raise ValueError(f"messages[{idx}] role='tool' 但缺少 tool_call_id")
                if tool_call_id:
                    norm_msg["tool_call_id"] = tool_call_id
                if not allow_tool_calls:
                    norm_msg["role"] = "user"
                    norm_msg.pop("tool_call_id", None)

            if "name" in msg and isinstance(msg["name"], str):
                norm_msg["name"] = msg["name"]

            if role == "assistant" and msg.get("reasoning"):
                norm_msg["reasoning"] = msg["reasoning"]
            if role == "assistant" and msg.get("reasoning_content"):
                norm_msg["reasoning_content"] = msg["reasoning_content"]

            normalized.append(norm_msg)

        return normalized

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        caller_name: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
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

        if self.reasoning_effort and self.reasoning_effort.lower() != "none":
            kwargs["reasoning_effort"] = self.reasoning_effort

        if self.preserve_thinking:
            kwargs["extra_body"] = {
                "chat_template_kwargs": {
                    "enable_thinking": True,
                    "preserve_thinking": True
                }
            }

        response = await self._client.chat.completions.create(**kwargs)
        return await self._parse_stream(response, caller_name, caller_uuid)

    async def _parse_stream(
        self,
        stream,
        caller_name: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        """解析 Chat Completions 流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_content_parts: list[str] = []
        finish_reason = None
        usage = None

        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            if not getattr(chunk, "choices", None):
                continue

            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                await self.emit_thinking_delta(reasoning, caller_name, caller_uuid)
            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content:
                reasoning_content_parts.append(reasoning_content)
                await self.emit_thinking_delta(reasoning_content, caller_name, caller_uuid)

            if delta.content:
                content_parts.append(delta.content)

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

        raw_content = "".join(content_parts)
        content = raw_content.strip() if tool_calls else raw_content
        reasoning = "".join(reasoning_parts)
        reasoning_content = "".join(reasoning_content_parts)

        if content:
            await self.emit_response_delta(content, caller_name, caller_uuid)

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
