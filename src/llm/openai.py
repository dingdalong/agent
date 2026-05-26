"""OpenAI SDK 实现的 LLM Provider — Responses API。"""

import logging
import time
import tiktoken
from openai import AsyncOpenAI
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Provider (Responses API)"""

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = True
        self._client = AsyncOpenAI(
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
        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        all_messages = (prompt or []) + messages
        messages_for_estimate = [{
            "messages": all_messages,
            "tools": tools,
        }] if tools else all_messages
        return len(encoding.encode(str(messages_for_estimate)))

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        if usage is None:
            return None
        input_token_details = getattr(usage, "input_token_details", None)
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(input_token_details, "cached_tokens", None),
            "cache_creation_input_tokens": getattr(input_token_details, "cache_creation_tokens", None),
        }

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role == "assistant" and msg.get("_response_output"):
            norm_msg["_response_output"] = msg["_response_output"]

    def _convert_to_input(
        self, messages: list[dict], prompt: list[dict] | None
    ) -> tuple[str | None, list[dict]]:
        """将 Chat Completions 格式的消息转换为 Responses API input 格式。"""
        instructions_parts: list[str] = []
        input_items: list[dict] = []

        for msg in (prompt or []) + messages:
            role = msg.get("role")

            if role in ("system", "developer"):
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                instructions_parts.append(content)

            elif role == "user":
                input_items.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                if "_response_output" in msg:
                    input_items.extend(msg["_response_output"])
                else:
                    if msg.get("content"):
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": msg["content"]}],
                        })
                    for tc in msg.get("tool_calls", []):
                        func = tc.get("function", {})
                        input_items.append({
                            "type": "function_call",
                            "call_id": tc["id"],
                            "name": func.get("name", ""),
                            "arguments": func.get("arguments", ""),
                        })

            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        instructions = "\n\n".join(instructions_parts) if instructions_parts else None
        return instructions, input_items

    def _convert_tools(self, tools: list[ToolDict] | None) -> list[dict] | None:
        """将 Chat Completions 工具格式转换为 Responses API 格式。"""
        if not tools:
            return None
        return [
            {
                "type": "function",
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "parameters": t["function"].get("parameters", {}),
                "strict": False,
            }
            for t in tools
        ]

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
        instructions, input_items = self._convert_to_input(messages, prompt)
        converted_tools = self._convert_tools(tools)

        kwargs: dict = {
            "model": self.model,
            "input": input_items,
            "stream": True,
            "temperature": temperature,
            "reasoning": {
                "effort": self.reasoning_effort,
                "summary": "auto",
            },
        }
        if instructions:
            kwargs["instructions"] = instructions
        if converted_tools:
            kwargs["tools"] = converted_tools
            kwargs["tool_choice"] = tool_choice or "auto"

        stream = await self._client.responses.create(**kwargs)
        return await self._parse_stream(stream, caller_agent_type, caller_uuid)

    async def _parse_stream(
        self,
        stream,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        """解析 Responses API 流式事件。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None
        usage = None
        output_items: list[dict] = []

        func_call_map: dict[str, int] = {}
        next_idx = 0

        async for event in stream:
            et = event.type

            if et == "response.output_text.delta":
                content_parts.append(event.delta)
                await self.emit_response_delta(event.delta, caller_agent_type, caller_uuid)

            elif et == "response.reasoning_summary_text.delta":
                reasoning_parts.append(event.delta)
                await self.emit_thinking_delta(event.delta, caller_agent_type, caller_uuid)

            elif et == "response.output_item.added":
                item = event.item
                if item.type == "function_call":
                    idx = next_idx
                    next_idx += 1
                    func_call_map[item.id] = idx
                    tool_calls[idx] = {
                        "id": item.call_id,
                        "name": item.name or "",
                        "arguments": "",
                    }

            elif et == "response.function_call_arguments.delta":
                idx = func_call_map.get(event.item_id)
                if idx is not None:
                    tool_calls[idx]["arguments"] += event.delta

            elif et == "response.completed":
                resp = event.response
                usage = getattr(resp, "usage", None)
                if resp.status == "completed":
                    finish_reason = "stop"
                elif resp.status == "incomplete":
                    finish_reason = "length"
                else:
                    finish_reason = resp.status
                for item in getattr(resp, "output", []) or []:
                    if hasattr(item, "model_dump"):
                        output_items.append(item.model_dump(exclude_none=True))
                    elif isinstance(item, dict):
                        output_items.append(item)

        content = "".join(content_parts)

        assistant_message: dict = {
            "role": "assistant",
            "content": content or None,
        }
        if output_items:
            assistant_message["_response_output"] = output_items
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
