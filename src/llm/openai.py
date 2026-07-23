"""OpenAI SDK 实现的 LLM Provider — Responses API。"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterable
import tiktoken
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

_OPENAI_RESPONSE_FINISH_REASONS = frozenset({"stop", "tool_calls"})


def _openai_stream_error(
    source: Any,
    *,
    fallback_message: str,
) -> LLMStreamResponseError:
    """从 Responses API 事件或响应提取有限错误元数据。

    Args:
        source: 携带 error、状态码或请求 ID 的事件或响应对象。
        fallback_message: provider 未提供 message 时使用的协议摘要。

    Returns:
        不携带完整响应对象的流响应异常。
    """
    error = getattr(source, "error", None) or source
    message = getattr(error, "message", None)
    code = getattr(error, "code", None) or getattr(error, "type", None)
    request_id = getattr(source, "request_id", None) or getattr(
        source,
        "_request_id",
        None,
    )
    status_code = getattr(source, "status_code", None)
    return LLMStreamResponseError(
        message if isinstance(message, str) and message else fallback_message,
        code=code if isinstance(code, str) else None,
        status_code=status_code if isinstance(status_code, int) else None,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _openai_refusal_error(source: Any) -> LLMStreamResponseError:
    """构造不包含拒绝正文的 Responses API 内容政策异常。

    Args:
        source: 可选携带请求 ID 的流事件或终态响应。

    Returns:
        provider code 固定为 refusal 的安全流响应异常。
    """
    request_id = getattr(source, "request_id", None) or getattr(
        source,
        "_request_id",
        None,
    )
    return LLMStreamResponseError(
        "Responses API 响应被拒绝",
        code="refusal",
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _contains_refusal_block(value: Any) -> bool:
    """递归判断 Responses API 最终输出是否含 refusal block。

    Args:
        value: model_dump 后的输出项、嵌套字段或标量值。

    Returns:
        任一嵌套字典的 type 为 refusal 时返回 True，否则返回 False。
    """
    if isinstance(value, dict):
        if value.get("type") == "refusal":
            return True
        return any(_contains_refusal_block(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_refusal_block(item) for item in value)
    return False


class OpenAIProvider(LLMProvider):
    """OpenAI Provider (Responses API)"""

    _EFFORT_DOWNGRADE = {"max": "xhigh", "xhigh": "high", "high": "medium", "medium": "low"}

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
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """向 Responses API 发起一次流式调用。

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
        caller_agent_type = call.caller_agent_type
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
                "effort": reasoning_effort_override or self.reasoning_effort,
                "summary": "auto",
            }
        if converted_tools:
            kwargs["tools"] = converted_tools
            kwargs["tool_choice"] = tool_choice or "auto"

        stream = await self._client.responses.create(**kwargs)
        return await self._parse_stream(stream, call=call)

    async def _parse_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """解析 Responses API 事件并即时记录工具片段。

        Args:
            stream: Responses API 异步事件流。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage = None
        output_items: list[dict] = []
        terminal_response: Any | None = None
        terminal_event_type: str | None = None

        func_call_map: dict[str, int] = {}
        next_idx = 0

        async for event in iter_llm_stream(stream):
            et = event.type
            if terminal_event_type is not None:
                raise LLMStreamResponseError(
                    "Responses API 终态后出现额外事件",
                    code="invalid_response",
                )

            if et == "response.output_text.delta":
                content_parts.append(event.delta)
                await self.emit_response_delta(event.delta, call=call)

            elif et == "response.reasoning_summary_text.delta":
                reasoning_parts.append(event.delta)
                await self.emit_thinking_delta(event.delta, call=call)

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
                    call.record_tool_fragment(
                        idx,
                        call_id=item.call_id or "",
                        name=item.name or "",
                    )

            elif et == "response.function_call_arguments.delta":
                idx = func_call_map.get(event.item_id)
                if idx is None:
                    raise LLMStreamResponseError(
                        "工具参数增量引用了未知输出项",
                        code="invalid_response",
                    )
                tool_calls[idx]["arguments"] += event.delta
                call.record_tool_fragment(idx, arguments=event.delta)

            elif et in {"response.refusal.delta", "response.refusal.done"}:
                raise _openai_refusal_error(event)

            elif et in {"error", "response.error"}:
                terminal_event_type = et
                raise _openai_stream_error(
                    event,
                    fallback_message="Responses API 返回流错误事件",
                )

            elif et == "response.failed":
                terminal_event_type = et
                raise _openai_stream_error(
                    event.response,
                    fallback_message="Responses API 响应失败",
                )

            elif et == "response.incomplete":
                terminal_event_type = et
                terminal_response = event.response
                details = getattr(terminal_response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                if reason == "max_output_tokens":
                    finish_reason = "length"
                elif reason == "content_filter":
                    raise LLMStreamResponseError(
                        "响应被内容政策过滤",
                        code="content_filter",
                        request_id=getattr(terminal_response, "_request_id", None),
                    )
                else:
                    raise LLMStreamResponseError(
                        "Responses API 返回未知 incomplete 原因",
                        code="invalid_response",
                        request_id=getattr(terminal_response, "_request_id", None),
                    )

            elif et == "response.completed":
                terminal_event_type = et
                terminal_response = event.response
                if getattr(terminal_response, "status", None) != "completed":
                    raise LLMStreamResponseError(
                        "Responses API completed 事件状态非法",
                        code="invalid_response",
                        request_id=getattr(terminal_response, "_request_id", None),
                    )
                finish_reason = "tool_calls" if tool_calls else "stop"

        if finish_reason is None or terminal_response is None:
            raise LLMStreamResponseError(
                "Responses API 流在合法终态前结束",
                code="invalid_response",
            )

        usage = getattr(terminal_response, "usage", None)
        for item in getattr(terminal_response, "output", []) or []:
            if hasattr(item, "model_dump"):
                output_items.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                output_items.append(item)

        if any(_contains_refusal_block(item) for item in output_items):
            raise _openai_refusal_error(terminal_response)

        if finish_reason != "length":
            validate_chat_completion_stream(
                finish_reason,
                tool_calls,
                valid_finish_reasons=_OPENAI_RESPONSE_FINISH_REASONS,
            )
            call.mark_tool_fragments_complete()

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

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            token_usage=self._extract_token_usage(usage),
        )
