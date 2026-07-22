"""Anthropic LLM Provider。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
from typing import TYPE_CHECKING, Any
import tiktoken
from anthropic import AsyncAnthropic
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

_ANTHROPIC_CACHEABLE_BLOCK_TYPES = frozenset({
    "text",
    "image",
    "document",
    "search_result",
    "tool_use",
    "tool_result",
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "code_execution_tool_result",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
    "tool_search_tool_result",
    "container_upload",
})


def _anthropic_stream_error(event: Any) -> LLMStreamResponseError:
    """从 Anthropic SSE error 事件提取有限错误元数据。

    Args:
        event: 携带 error 与可选请求 ID 的 SSE 事件。

    Returns:
        不携带完整事件对象的流响应异常。
    """
    error = getattr(event, "error", None)
    message = getattr(error, "message", None)
    code = getattr(error, "code", None) or getattr(error, "type", None)
    request_id = getattr(event, "request_id", None)
    status_code = getattr(event, "status_code", None)
    return LLMStreamResponseError(
        message if isinstance(message, str) and message else "Anthropic 返回 SSE error 事件",
        code=code if isinstance(code, str) else None,
        status_code=status_code if isinstance(status_code, int) else None,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _dump_anthropic_content_block(block: Any) -> dict[str, Any]:
    """把 Anthropic SDK 最终内容块完整序列化为字典。

    Args:
        block: SDK 内容块，或测试使用的字典/简单对象 double。

    Returns:
        排除 None 字段后的独立内容块字典。

    Raises:
        LLMStreamResponseError: 内容块无法序列化为字典。
    """
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
    elif isinstance(block, dict):
        dumped = deepcopy(block)
    else:
        fields = getattr(block, "__dict__", None)
        if not isinstance(fields, dict):
            raise LLMStreamResponseError(
                "Anthropic 最终消息 content block 类型非法",
                code="invalid_response",
            )
        dumped = {
            key: deepcopy(value)
            for key, value in fields.items()
            if not key.startswith("_") and value is not None
        }
    if not isinstance(dumped, dict):
        raise LLMStreamResponseError(
            "Anthropic content block 序列化结果类型非法",
            code="invalid_response",
        )
    return dumped


class AnthropicProvider(LLMProvider):
    """Anthropic Provider (Messages API)"""

    @classmethod
    async def list_models(
        cls,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        user_agent: str = "",
    ) -> list[str]:
        """从 Anthropic Models API 获取全部模型 ID。

        Args:
            api_key: Anthropic API 密钥。
            base_url: Anthropic API 根地址。
            timeout: SDK 请求与外层等待的超时秒数。
            user_agent: 可选自定义 User-Agent。

        Returns:
            分页获取的全部模型 ID。
        """
        client = AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0,
            default_headers=cls._ua_headers(user_agent),
        )
        try:
            async def _fetch() -> list[str]:
                """分页读取 Anthropic 模型。

                Returns:
                    分页合并后的模型 ID 列表。
                """
                models: list[str] = []
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
            default_headers=self._ua_headers(self.user_agent),
        )

    def protocol_continuation_limit(self, finish_reason: str) -> int:
        """查询 Anthropic 指定协议终态允许的自动续接次数。

        Args:
            finish_reason: provider 归一化后的终态原因。

        Returns:
            pause_turn 返回实例配置值，其他终态返回 0。
        """
        if finish_reason == "pause_turn":
            return self.max_pause_turn_continuations
        return 0

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """估算 Messages API 实际输入内容的 token 数。

        Args:
            messages: OpenAI 兼容格式的对话消息列表。
            prompt: 可选的系统提示词消息列表。
            tools: 可选的 OpenAI function-calling 工具 schema 列表。

        Returns:
            转换后 system、messages 与工具 schema 的估算 token 数。
        """
        system, claude_messages = self._convert_messages(messages, prompt)
        claude_messages = deepcopy(claude_messages)
        self._apply_cache_control(claude_messages)
        payload: dict[str, object] = {"messages": claude_messages}
        if system:
            payload["system"] = self._system_blocks(system)
        claude_tools = self._convert_tools(tools)
        if claude_tools:
            payload["tools"] = claude_tools
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return len(str(payload)) // 4
        return len(encoding.encode(str(payload)))

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
        """将 OpenAI 兼容格式消息转换为 Claude Messages API 格式。

        Args:
            messages: 待转换的会话消息列表。
            prompt: 可选系统提示词消息列表。

        Returns:
            合并后的可选系统文本与独立的 Claude 消息列表。
        """
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
                        "content": deepcopy(msg["_anthropic_content"]),
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

    def _system_blocks(self, system: str) -> list[dict]:
        """把系统提示词包成带缓存断点的单个文本块。

        Anthropic 缓存排序为 tools → system → messages，system 断点即缓存 tools+system
        整个稳定前缀。

        Args:
            system: 合并后的系统提示词文本。

        Returns:
            含单个 text 块的列表，该块带 ephemeral cache_control 断点。
        """
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _apply_cache_control(self, messages: list[dict]) -> None:
        """给最后一条消息中最后一个 SDK 允许的内容块添加缓存断点。

        随对话增长逐轮增量缓存对话前缀：本轮在末块写断点，上一轮的断点即成为读命中。
        content 为字符串时先包成单个 text 块；禁用或未知块会被跳过。

        Args:
            messages: 已转换为 Claude 格式的消息列表（就地修改其末条）。

        Returns:
            无（就地修改 messages）。
        """
        if not messages:
            return
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
            last["content"] = content
        if not isinstance(content, list):
            return
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if (
                isinstance(block, dict)
                and block.get("type") in _ANTHROPIC_CACHEABLE_BLOCK_TYPES
            ):
                content[index] = {
                    **block,
                    "cache_control": {"type": "ephemeral"},
                }
                return

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
        enable_thinking: bool = True,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """向 Anthropic Messages API 发起一次流式调用。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词列表。
            tools: 可选工具 schema 列表。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        system, claude_messages = self._convert_messages(messages, prompt)
        claude_tools = self._convert_tools(tools)
        self._apply_cache_control(claude_messages)

        kwargs: dict = {
            "model": self.model,
            "max_tokens": 16000,
            "messages": claude_messages,
        }

        if enable_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
            effort = self._map_effort(self.reasoning_effort)
            kwargs["output_config"] = {"effort": effort}
        else:
            kwargs["thinking"] = {"type": "disabled"}

        if system:
            kwargs["system"] = self._system_blocks(system)
        if claude_tools:
            kwargs["tools"] = claude_tools
            kwargs["tool_choice"] = self._convert_tool_choice(
                tool_choice or "auto"
            )

        return await self._stream_chat(
            call=call,
            **kwargs,
        )

    async def _stream_chat(
        self,
        *,
        call: LLMCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行流式调用并即时记录正文、思考和工具片段。

        Args:
            call: 当前独立调用尝试上下文。
            kwargs: 下发给 Anthropic Messages API 的请求参数。

        Returns:
            归一化后的 LLM 响应。
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        client_tool_indices: set[int] = set()
        saw_message_stop = False

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in iter_llm_stream(stream):
                if saw_message_stop:
                    raise LLMStreamResponseError(
                        "Anthropic message_stop 后出现额外事件",
                        code="invalid_response",
                    )
                if event.type == "error":
                    error = getattr(event, "error", None)
                    if isinstance(error, BaseException):
                        raise error
                    raise _anthropic_stream_error(event)
                if event.type == "message_stop":
                    saw_message_stop = True
                    continue
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        client_tool_indices.add(event.index)
                        call.record_tool_fragment(
                            event.index,
                            call_id=block.id or "",
                            name=block.name or "",
                        )
                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        thinking_parts.append(event.delta.thinking)
                        await self.emit_thinking_delta(event.delta.thinking, call=call)
                    elif event.delta.type == "text_delta":
                        content_parts.append(event.delta.text)
                        await self.emit_response_delta(event.delta.text, call=call)
                    elif (
                        event.delta.type == "input_json_delta"
                        and event.index in client_tool_indices
                    ):
                        call.record_tool_fragment(
                            event.index,
                            arguments=event.delta.partial_json or "",
                        )

            final = await stream.get_final_message()

        if not saw_message_stop:
            raise LLMStreamResponseError(
                "Anthropic 流未收到 message_stop 终态事件",
                code="invalid_response",
            )
        if final is None:
            raise LLMStreamResponseError(
                "Anthropic 流未返回最终消息",
                code="invalid_response",
            )

        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        response = self._build_response(final, content, thinking)
        if response.finish_reason != "length":
            call.mark_tool_fragments_complete()
        return response

    def _build_response(
        self,
        message: Any,
        content: str,
        thinking: str,
    ) -> LLMResponse:
        """从 Claude 最终消息构建并校验统一响应。

        Args:
            message: Anthropic SDK 返回的最终消息。
            content: 流中累计的正文。
            thinking: 流中累计的思考文本。

        Returns:
            终态与工具调用均合法的统一响应。

        Raises:
            LLMStreamResponseError: stop_reason 或工具调用违反响应协议。
        """
        tool_calls: dict[int, dict[str, str]] = {}
        anthropic_content: list[dict] = []
        idx = 0

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason in {"end_turn", "stop_sequence"}:
            finish_reason = "stop"
        elif stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        elif stop_reason == "pause_turn":
            finish_reason = "pause_turn"
        elif stop_reason == "model_context_window_exceeded":
            raise LLMStreamResponseError(
                "Anthropic 模型上下文窗口已满",
                code="model_context_window_exceeded",
            )
        elif stop_reason in {"refusal", "content_filter"}:
            raise LLMStreamResponseError(
                "Anthropic 响应被拒绝或内容政策过滤",
                code=stop_reason,
            )
        else:
            raise LLMStreamResponseError(
                "Anthropic 最终消息缺少合法 stop_reason",
                code="invalid_response",
            )

        message_content = getattr(message, "content", None)
        if not isinstance(message_content, list):
            raise LLMStreamResponseError(
                "Anthropic 最终消息 content 类型非法",
                code="invalid_response",
            )

        for block in message_content:
            dumped_block = _dump_anthropic_content_block(block)
            anthropic_content.append(dumped_block)
            if dumped_block.get("type") == "tool_use":
                tool_input = dumped_block.get("input")
                if finish_reason != "length":
                    if not isinstance(tool_input, dict):
                        raise LLMStreamResponseError(
                            "Anthropic 工具调用 input 必须是 object",
                            code="invalid_response",
                        )
                tool_calls[idx] = {
                    "id": dumped_block.get("id"),
                    "name": dumped_block.get("name"),
                    "arguments": json.dumps(tool_input, ensure_ascii=False),
                }
                idx += 1

        if finish_reason == "pause_turn" and not anthropic_content:
            raise LLMStreamResponseError(
                "Anthropic pause_turn 缺少可续接的 content block",
                code="invalid_response",
            )

        if finish_reason == "pause_turn" and tool_calls:
            raise LLMStreamResponseError(
                "Anthropic pause_turn 不得包含客户端 tool_use",
                code="invalid_response",
            )

        validate_chat_completion_stream(
            finish_reason,
            tool_calls,
            valid_finish_reasons={"stop", "tool_calls", "length", "pause_turn"},
        )

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

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            token_usage=self._extract_token_usage(getattr(message, "usage", None)),
        )
