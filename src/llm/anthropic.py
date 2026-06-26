"""Anthropic LLM Provider。"""

import asyncio
import json
import logging
import tiktoken
from anthropic import AsyncAnthropic
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic Provider (Messages API)"""

    @classmethod
    async def list_models(cls, api_key: str, base_url: str, timeout: float = 3.0) -> list[str]:
        client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=3.0, max_retries=0)
        try:
            async def _fetch() -> list[str]:
                models = []
                page = await client.models.list(limit=100)
                models.extend(m.id for m in page.data)
                while page.has_more:
                    page = await page.get_next_page()
                    models.extend(m.id for m in page.data)
                return models
            return await asyncio.wait_for(_fetch(), timeout=timeout)
        finally:
            await client.close()

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = True
        self._client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
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
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return len(str(messages_for_estimate)) // 4
        return len(encoding.encode(str(messages_for_estimate)))

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        """把 Anthropic 原生 usage 归一化为统一约定：input_tokens = 提交给模型的全部输入 token。

        Anthropic 的 usage.input_tokens 仅为未命中缓存的新算输入，缓存读取/写入单独列字段；
        为与 DeepSeek/OpenAI/Ollama 的 prompt_tokens（本就含缓存）口径一致，这里把三者相加作为
        input_tokens，使上层（状态条等）无需感知 provider 差异即可正确统计总输入与命中率。

        Args:
            usage: Anthropic SDK 返回的 usage 对象，可能为 None。

        Returns:
            统一口径的 token 字典；usage 为 None 时返回 None。cache_read/cache_creation 保持原值单列。
        """
        if usage is None:
            return None
        raw_input = getattr(usage, "input_tokens", None) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        return {
            "input_tokens": raw_input + (cache_read or 0) + (cache_creation or 0),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }

    def clear_reasoning_content(self, messages):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                msg.pop("reasoning_content", None)
                if "_anthropic_content" in msg:
                    msg["_anthropic_content"] = [
                        b for b in msg["_anthropic_content"]
                        if b.get("type") != "thinking"
                    ]

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role != "assistant":
            return
        if msg.get("reasoning_content"):
            norm_msg["reasoning_content"] = msg["reasoning_content"]
        if msg.get("_anthropic_content"):
            norm_msg["_anthropic_content"] = msg["_anthropic_content"]

    # ---- 格式转换 ----

    def _convert_tools(self, tools: list[ToolDict] | None) -> list[dict] | None:
        """将 OpenAI 工具格式转换为 Claude 格式。"""
        if not tools:
            return None
        return [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            for t in tools
        ]

    def _convert_tool_choice(self, tool_choice: str | dict | None) -> dict:
        """将 OpenAI tool_choice 格式转换为 Claude 格式。"""
        if tool_choice is None or tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice in ("any", "required"):
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "auto"}
        if isinstance(tool_choice, dict):
            name = tool_choice.get("function", {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        return {"type": "auto"}

    def _convert_messages(
        self, messages: list[dict], prompt: list[dict] | None
    ) -> tuple[str | None, list[dict]]:
        """将 OpenAI 兼容格式消息转换为 Claude Messages API 格式。"""
        system_parts: list[str] = []
        claude_messages: list[dict] = []

        for msg in (prompt or []) + messages:
            role = msg.get("role")

            if role in ("system", "developer"):
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                if content:
                    system_parts.append(content)

            elif role == "user":
                claude_messages.append({
                    "role": "user",
                    "content": msg.get("content", ""),
                })

            elif role == "assistant":
                if "_anthropic_content" in msg and msg["_anthropic_content"]:
                    claude_messages.append({
                        "role": "assistant",
                        "content": msg["_anthropic_content"],
                    })
                else:
                    content_blocks = []
                    if msg.get("content"):
                        content_blocks.append({
                            "type": "text",
                            "text": msg["content"],
                        })
                    for tc in msg.get("tool_calls", []):
                        func = tc.get("function", {})
                        try:
                            input_data = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            input_data = {}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": func.get("name", ""),
                            "input": input_data,
                        })
                    if content_blocks:
                        claude_messages.append({
                            "role": "assistant",
                            "content": content_blocks,
                        })

            elif role == "tool":
                claude_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })

        merged = self._merge_messages(claude_messages)
        system = "\n\n".join(system_parts) if system_parts else None
        return system, merged

    def _merge_messages(self, messages: list[dict]) -> list[dict]:
        """合并连续的同角色消息（Claude API 要求严格交替 user/assistant）。"""
        if not messages:
            return []

        merged: list[dict] = [messages[0]]

        for msg in messages[1:]:
            if msg["role"] == merged[-1]["role"]:
                prev = merged[-1]["content"]
                curr = msg["content"]

                if isinstance(prev, str):
                    prev = [{"type": "text", "text": prev}]
                if isinstance(curr, str):
                    curr = [{"type": "text", "text": curr}]
                if not isinstance(prev, list):
                    prev = []
                if not isinstance(curr, list):
                    curr = []

                merged[-1]["content"] = prev + curr
            else:
                merged.append(msg)

        return merged

    # ---- 推理力度映射 ----

    def _map_effort(self, reasoning_effort: str) -> str:
        """将配置中的推理力度映射为 Claude effort 参数。"""
        effort = reasoning_effort.lower()
        if effort == "max" and "sonnet" in self.model:
            return "high"
        if effort == "xhigh":
            return "high"
        valid = {"low", "medium", "high", "max"}
        return effort if effort in valid else "high"

    # ---- API 调用 ----

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
        system, claude_messages = self._convert_messages(messages, prompt)
        claude_tools = self._convert_tools(tools)

        kwargs: dict = {
            "model": self.model,
            "max_tokens": 16000,
            "messages": claude_messages,
            "thinking": {"type": "adaptive"},
        }

        effort = self._map_effort(self.reasoning_effort)
        kwargs["output_config"] = {"effort": effort}

        if system:
            kwargs["system"] = system
        if claude_tools:
            kwargs["tools"] = claude_tools
            kwargs["tool_choice"] = self._convert_tool_choice(
                tool_choice or "auto"
            )

        return await self._stream_chat(
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
            **kwargs,
        )

    async def _stream_chat(
        self,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        **kwargs,
    ) -> LLMResponse:
        """执行流式调用并解析响应。"""
        content_parts: list[str] = []
        thinking_parts: list[str] = []

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        thinking_parts.append(event.delta.thinking)
                        await self.emit_thinking_delta(event.delta.thinking, caller_agent_type, caller_uuid)
                    elif event.delta.type == "text_delta":
                        content_parts.append(event.delta.text)
                        await self.emit_response_delta(event.delta.text, caller_agent_type, caller_uuid)

            final = await stream.get_final_message()

        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        return self._build_response(final, content, thinking)

    def _build_response(
        self, message, content: str, thinking: str
    ) -> LLMResponse:
        """从 Claude 响应构建 LLMResponse。"""
        tool_calls: dict[int, dict[str, str]] = {}
        anthropic_content: list[dict] = []
        idx = 0

        for block in message.content:
            if block.type == "text":
                anthropic_content.append({
                    "type": "text",
                    "text": block.text,
                })
            elif block.type == "thinking":
                anthropic_content.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": getattr(block, "signature", ""),
                })
            elif block.type == "tool_use":
                tool_calls[idx] = {
                    "id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                }
                anthropic_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                idx += 1

        stop_reason = message.stop_reason
        if stop_reason == "end_turn":
            finish_reason = "stop"
        elif stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = stop_reason

        assistant_message: dict = {
            "role": "assistant",
            "content": content or None,
            "_anthropic_content": anthropic_content,
        }
        if thinking:
            assistant_message["reasoning_content"] = thinking
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
            token_usage=self._extract_token_usage(getattr(message, "usage", None)),
        )
