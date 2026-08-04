"""DeepSeek Responses API LLM Provider。"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

import transformers
from openai import AsyncOpenAI

from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.responses import (
    ResponsesStreamMixin,
    convert_function_tools,
    convert_tool_choice,
)

if TYPE_CHECKING:
    from src.tools import ToolDict


class DeepSeekProvider(ResponsesStreamMixin, LLMProvider):
    """基于 OpenAI SDK Responses API 的 DeepSeek Provider。"""

    _EFFORT_DOWNGRADE = {"max": "high"}
    _RESPONSE_REASONING_DELTA_EVENTS = frozenset({"response.reasoning_text.delta"})

    def __post_init__(self) -> None:
        super().__post_init__()
        self.supports_native_structured_output = True
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
            default_headers=self._ua_headers(self.user_agent),
        )

    def clear_reasoning_content(self, messages: list[Any]) -> None:
        """清除无工具轮次的已知思考载体并保留其他输出 item。"""
        for message in messages:
            if not isinstance(message, dict):
                if hasattr(message, "reasoning_content"):
                    message.reasoning_content = None
                continue
            message.pop("reasoning_content", None)
            output_items = message.get("_response_output")
            if not isinstance(output_items, list):
                continue
            retained_items = [
                item
                for item in output_items
                if not isinstance(item, dict) or item.get("type") != "reasoning"
            ]
            if retained_items:
                message["_response_output"] = retained_items
            else:
                message.pop("_response_output", None)

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """估算 DeepSeek Responses API 实际请求载荷的 token 数。"""
        instructions, input_items = self._convert_to_input(messages, prompt)
        payload: dict[str, object] = {"input": input_items}
        if instructions:
            payload["instructions"] = instructions
        converted_tools = convert_function_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
        return len(self._tokenizer.encode(str(payload)))

    @cached_property
    def _tokenizer(self) -> Any:
        return transformers.AutoTokenizer.from_pretrained(
            "src/llm/tokenizer/deepseek", trust_remote_code=True
        )

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        """把 Responses usage 归一为框架公共 token 字段。"""
        if usage is None:
            return None
        input_details = getattr(usage, "input_tokens_details", None)
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(input_details, "cached_tokens", None),
            "cache_creation_input_tokens": None,
        }

    def _normalize_content(self, content: Any) -> str | list[dict]:
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

    def _normalize_assistant_extra(
        self,
        msg: dict,
        norm_msg: dict,
        role: str,
    ) -> None:
        if role == "assistant" and msg.get("_response_output"):
            norm_msg["_response_output"] = msg["_response_output"]

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else str(content)
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    @staticmethod
    def _message_content(content: Any, *, block_type: str) -> Any:
        if not isinstance(content, list):
            return content
        converted: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                converted.append(part)
                continue
            if part.get("type") == "text":
                converted.append({"type": block_type, "text": part.get("text", "")})
            else:
                converted.append(part)
        return converted

    def _convert_to_input(
        self,
        messages: list[dict],
        prompt: list[dict] | None,
    ) -> tuple[str, list[dict]]:
        """将框架消息转换为无状态 Responses instructions 与 input items。"""
        instructions_parts: list[str] = []
        input_items: list[dict] = []

        for message in (prompt or []) + messages:
            role = message.get("role")
            if role in {"system", "developer"}:
                text = self._content_text(message.get("content", ""))
                if text:
                    instructions_parts.append(text)
            elif role == "user":
                input_items.append({
                    "role": "user",
                    "content": self._message_content(
                        message.get("content", ""),
                        block_type="input_text",
                    ),
                })
            elif role == "assistant":
                response_output = message.get("_response_output")
                if isinstance(response_output, list):
                    input_items.extend(response_output)
                    continue
                content = message.get("content")
                if content:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": self._message_content(
                            content,
                            block_type="output_text",
                        ),
                    })
                for tool_call in message.get("tool_calls", []):
                    function = tool_call.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", ""),
                    })
            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content", ""),
                })

        return "\n\n".join(instructions_parts), input_items

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
        """向 DeepSeek Responses API 发起一次流式调用。"""
        instructions, input_items = self._convert_to_input(messages, prompt)
        converted_tools = convert_function_tools(tools)
        request: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "temperature": temperature,
        }
        if instructions:
            request["instructions"] = instructions
        if input_items:
            request["input"] = input_items
        if enable_thinking:
            request["reasoning"] = {
                "effort": reasoning_effort_override or self.reasoning_effort
            }
        if converted_tools:
            request["tools"] = converted_tools
            request["tool_choice"] = convert_tool_choice(tool_choice) or "auto"

        stream = await self._client.responses.create(**request)
        return await self._parse_stream(stream, call=call)
