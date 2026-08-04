"""Responses API provider 共用的请求与流式协议辅助。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterable, ClassVar

from src.llm.base import iter_llm_stream, validate_chat_completion_stream
from src.llm.errors import LLMStreamResponseError

if TYPE_CHECKING:
    from src.llm.base import LLMCallContext, LLMResponse
    from src.tools import ToolDict


_RESPONSE_FINISH_REASONS = frozenset({"stop", "tool_calls"})


def response_stream_error(
    source: Any,
    *,
    fallback_message: str,
) -> LLMStreamResponseError:
    """从 Responses API 事件或响应提取有限错误元数据。"""
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


def convert_function_tools(tools: list[ToolDict] | None) -> list[dict] | None:
    """将 Chat Completions function schema 转换为 Responses 格式。"""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "parameters": tool["function"].get("parameters", {}),
            "strict": False,
        }
        for tool in tools
    ]


def convert_tool_choice(tool_choice: str | dict | None) -> str | dict | None:
    """把框架使用的旧 function 选择格式转换为 Responses 格式。

    仅转换可明确识别的 Chat Completions function 结构；其余值原样透传，
    以便 provider 后续新增的 tool choice 类型无需修改本层。
    """
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return tool_choice
    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return tool_choice
    name = function.get("name")
    if not isinstance(name, str):
        return tool_choice
    return {"type": "function", "name": name}


class ResponsesStreamMixin:
    """将 Responses SSE 事件归一为框架公共响应。"""

    _RESPONSE_REASONING_DELTA_EVENTS: ClassVar[frozenset[str]] = frozenset()
    _RESPONSE_REFUSAL_EVENTS: ClassVar[frozenset[str]] = frozenset()

    def _response_refusal_error(self, source: Any) -> LLMStreamResponseError:
        """构造 provider 对应的拒绝异常。"""
        return LLMStreamResponseError(
            "Responses API 响应被拒绝",
            code="refusal",
            request_id=getattr(source, "request_id", None),
        )

    def _validate_response_output(
        self,
        output_items: list[dict],
        terminal_response: Any,
    ) -> None:
        """执行 provider 专属的最终输出校验。"""

    async def _parse_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """解析 Responses API 事件并即时记录正文、思考和工具片段。"""
        from src.llm.base import LLMResponse

        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        terminal_response: Any | None = None
        terminal_event_type: str | None = None
        finish_reason: str | None = None
        output_items: list[dict] = []

        function_call_indexes: dict[str, int] = {}
        next_index = 0

        async for event in iter_llm_stream(stream):
            event_type = event.type
            if terminal_event_type is not None:
                raise LLMStreamResponseError(
                    "Responses API 终态后出现额外事件",
                    code="invalid_response",
                )

            if event_type == "response.output_text.delta":
                content_parts.append(event.delta)
                await self.emit_response_delta(event.delta, call=call)

            elif event_type in self._RESPONSE_REASONING_DELTA_EVENTS:
                await self.emit_thinking_delta(event.delta, call=call)

            elif event_type == "response.output_item.added":
                item = event.item
                if item.type == "function_call":
                    item_id = getattr(item, "id", None)
                    if not isinstance(item_id, str) or not item_id:
                        raise LLMStreamResponseError(
                            "工具输出项缺少有效 ID",
                            code="invalid_response",
                        )
                    if item_id in function_call_indexes:
                        raise LLMStreamResponseError(
                            "工具输出项 ID 重复",
                            code="invalid_response",
                        )
                    index = next_index
                    next_index += 1
                    function_call_indexes[item_id] = index
                    call_id = getattr(item, "call_id", None) or ""
                    name = getattr(item, "name", None) or ""
                    arguments = getattr(item, "arguments", None) or ""
                    tool_calls[index] = {
                        "id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                    call.record_tool_fragment(
                        index,
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )

            elif event_type == "response.function_call_arguments.delta":
                index = function_call_indexes.get(event.item_id)
                if index is None:
                    raise LLMStreamResponseError(
                        "工具参数增量引用了未知输出项",
                        code="invalid_response",
                    )
                tool_calls[index]["arguments"] += event.delta
                call.record_tool_fragment(index, arguments=event.delta)

            elif event_type == "response.function_call_arguments.done":
                index = function_call_indexes.get(event.item_id)
                if index is None:
                    raise LLMStreamResponseError(
                        "工具参数终态引用了未知输出项",
                        code="invalid_response",
                    )
                arguments = getattr(event, "arguments", None)
                if isinstance(arguments, str):
                    accumulated = tool_calls[index]["arguments"]
                    if accumulated and accumulated != arguments:
                        raise LLMStreamResponseError(
                            "工具参数增量与终态内容不一致",
                            code="invalid_response",
                        )
                    if not accumulated:
                        tool_calls[index]["arguments"] = arguments
                        call.record_tool_fragment(index, arguments=arguments)

            elif event_type in self._RESPONSE_REFUSAL_EVENTS:
                raise self._response_refusal_error(event)

            elif event_type in {"error", "response.error"}:
                terminal_event_type = event_type
                raise response_stream_error(
                    event,
                    fallback_message="Responses API 返回流错误事件",
                )

            elif event_type == "response.failed":
                terminal_event_type = event_type
                raise response_stream_error(
                    event.response,
                    fallback_message="Responses API 响应失败",
                )

            elif event_type == "response.incomplete":
                terminal_event_type = event_type
                terminal_response = event.response
                if getattr(terminal_response, "status", None) != "incomplete":
                    raise LLMStreamResponseError(
                        "Responses API incomplete 事件状态非法",
                        code="invalid_response",
                        request_id=getattr(terminal_response, "_request_id", None),
                    )
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

            elif event_type == "response.completed":
                terminal_event_type = event_type
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

        for item in getattr(terminal_response, "output", []) or []:
            if hasattr(item, "model_dump"):
                output_items.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                output_items.append(item)
        self._validate_response_output(output_items, terminal_response)

        if finish_reason != "length":
            validate_chat_completion_stream(
                finish_reason,
                tool_calls,
                valid_finish_reasons=_RESPONSE_FINISH_REASONS,
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
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": tool_call["arguments"],
                    },
                }
                for tool_call in tool_calls.values()
            ]

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
            token_usage=self._extract_token_usage(
                getattr(terminal_response, "usage", None)
            ),
        )
