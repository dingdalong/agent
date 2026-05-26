"""DeepSeek LLM Provider。"""

import logging
from functools import cached_property
from openai import AsyncOpenAI
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict
import transformers

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
            max_retries=0,
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
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=prompt + messages if prompt is not None else messages,
            tools=tools,
            stream=True,
            stream_options={"include_usage": True},
            temperature=temperature,
            tool_choice=tool_choice or ("auto" if tools else None),
            reasoning_effort=self.reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}}
        )
        return await self._parse_stream(
            response,
            caller_agent_type,
            caller_uuid,
        )

    async def _parse_stream(
        self,
        stream,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        """解析流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None
        usage = None

        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            if not getattr(chunk, "choices", None):
                continue

            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is not None:
                reasoning_parts.append(reasoning)
                await self.emit_thinking_delta(reasoning, caller_agent_type, caller_uuid)

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
                    content_parts.append(delta.content)
                    await self.emit_response_delta(delta.content, caller_agent_type, caller_uuid)

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

        content = "".join(content_parts) or ""
        reasoning_content = "".join(reasoning_parts) or ""

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
