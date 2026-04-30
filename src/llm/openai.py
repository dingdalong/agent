"""OpenAI SDK 实现的 LLM Provider — Responses API。"""

import logging
import time
import tiktoken
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, InternalServerError
from src.events.types import ResponseDelta, ThinkingDelta
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Provider (Responses API)"""

    _retryable_errors = (APIConnectionError, RateLimitError, InternalServerError)

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = True
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def estimate_tokens(self, messages: list[dict]) -> int:
        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        return len(encoding.encode(str(messages)))

    def micro_compact(self, messages: list[dict], keep_recent: int) -> list[dict]:
        tool_msgs = [(i, msg) for i, msg in enumerate(messages)
                     if msg.get("role") == "tool"]
        if len(tool_msgs) <= keep_recent:
            return messages
        for _, msg in tool_msgs[:-keep_recent]:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 120:
                msg["content"] = "[Earlier tool result compacted. Re-run the tool if you need full detail.]"
        return messages

    def normalize_messages(
        self,
        messages: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False,
    ) -> list[dict]:
        VALID_ROLES = {"system", "user", "assistant", "tool"}
        if allow_developer_role:
            VALID_ROLES.add("developer")

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

            normalized.append(norm_msg)

        return normalized

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
        return await self._parse_stream(stream)

    async def _parse_stream(self, stream) -> LLMResponse:
        """解析 Responses API 流式事件。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None

        func_call_map: dict[str, int] = {}
        next_idx = 0

        async for event in stream:
            et = event.type

            if et == "response.output_text.delta":
                content_parts.append(event.delta)
                await self.event_bus.emit(ResponseDelta(
                    timestamp=time.time(),
                    source=self.model,
                    content=event.delta,
                ))

            elif et == "response.reasoning_summary_text.delta":
                reasoning_parts.append(event.delta)
                await self.event_bus.emit(ThinkingDelta(
                    timestamp=time.time(),
                    source=self.model,
                    content=event.delta,
                ))

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
                if resp.status == "completed":
                    finish_reason = "stop"
                elif resp.status == "incomplete":
                    finish_reason = "length"
                else:
                    finish_reason = resp.status

        content = "".join(content_parts)

        assistant_message: dict = {
            "role": "assistant",
            "content": content or None,
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

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
        )
