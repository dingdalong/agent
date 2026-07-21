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
            default_headers=self._ua_headers(self.user_agent),
        )

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """估算 Responses API 实际输入内容的 token 数。

        Args:
            messages: OpenAI 兼容格式的对话消息列表。
            prompt: 可选的系统提示词消息列表。
            tools: 可选的 OpenAI function-calling 工具 schema 列表。

        Returns:
            转换后 input 项与工具 schema 的估算 token 数。
        """
        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        payload: dict[str, object] = {
            "input": self._convert_to_input(messages, prompt),
        }
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
        return len(encoding.encode(str(payload)))

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        """把 Responses API 的 usage 归一化为统一 token 字典。

        Responses API 的 usage.input_tokens 本就含缓存读取部分（与 DeepSeek/Ollama 的
        prompt_tokens 口径一致），故直接用作 input_tokens。命中缓存的读取量在
        usage.input_tokens_details.cached_tokens；该 SDK 的 InputTokensDetails 不含缓存写入
        字段，cache_creation_input_tokens 恒为 None。

        Args:
            usage: OpenAI SDK 返回的 ResponseUsage 对象，可能为 None。

        Returns:
            统一口径的 token 字典；usage 为 None 时返回 None。
        """
        if usage is None:
            return None
        input_tokens_details = getattr(usage, "input_tokens_details", None)
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(input_tokens_details, "cached_tokens", None),
            "cache_creation_input_tokens": None,
        }

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role == "assistant" and msg.get("_response_output"):
            norm_msg["_response_output"] = msg["_response_output"]

    def _convert_to_input(
        self, messages: list[dict], prompt: list[dict] | None
    ) -> list[dict]:
        """将 Chat Completions 格式的消息转换为 Responses API input 项列表。

        系统/开发者提示词不写入 instructions 字段，而是合并为首条 developer 消息置于
        input 首位——GPT-5 系列在 Responses API 上不缓存 instructions 里的系统提示词，
        改走 input 首条 developer 消息才能进入可缓存前缀。

        Args:
            messages: Chat Completions 格式的对话消息列表。
            prompt: 系统提示词消息列表（可为 None），拼在 messages 之前一并转换。

        Returns:
            Responses API 的 input 项列表；首条为承载系统提示词的 developer 消息（若有）。
        """
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

        instructions = "\n\n".join(instructions_parts)
        if instructions:
            input_items.insert(0, {"role": "developer", "content": instructions})
        return input_items

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
        enable_thinking: bool = True,
    ) -> LLMResponse:
        input_items = self._convert_to_input(messages, prompt)
        converted_tools = self._convert_tools(tools)

        kwargs: dict = {
            "model": self.model,
            "input": input_items,
            "stream": True,
            "temperature": temperature,
            # 跨重启稳定的路由键：同模型 + 同 agent 类型 → 同键，使稳定的系统提示词前缀
            # 在 TTL 内可跨会话/重启复用（不含每进程随机的 uuid）。GPT-5.6+ 需此键才能可靠命中。
            "prompt_cache_key": f"{self.model}:{caller_agent_type}" if caller_agent_type else self.model,
        }
        if enable_thinking:
            kwargs["reasoning"] = {
                "effort": self.reasoning_effort,
                "summary": "auto",
            }
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
