"""DeepSeek Responses API 请求转换与流式协议测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from src.llm.base import LLMCallContext
from src.llm.deepseek import DeepSeekProvider
from src.llm.errors import LLMErrorKind, LLMStreamResponseError, classify_llm_error


class FakeAsyncStream:
    """按顺序产生 Responses 事件并可在末尾抛出异常。"""

    def __init__(
        self,
        events: list[Any],
        *,
        terminal_error: BaseException | None = None,
    ) -> None:
        self._events = list(events)
        self._terminal_error = terminal_error

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        if self._events:
            return self._events.pop(0)
        if self._terminal_error is not None:
            raise self._terminal_error
        raise StopAsyncIteration


class CapturingCreate:
    """记录 Responses create 请求并返回预设流。"""

    def __init__(self, stream: FakeAsyncStream) -> None:
        self.stream = stream
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeAsyncStream:
        self.requests.append(kwargs)
        return self.stream


class DumpableItem:
    """模拟 SDK 输出 item。"""

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def model_dump(self, *, exclude_none: bool) -> dict[str, Any]:
        assert exclude_none is True
        return self.value


def _call() -> LLMCallContext:
    return LLMCallContext(
        attempt=1,
        caller_agent_type="worker",
        caller_uuid="uuid-deepseek",
    )


def _event(event_type: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, **fields)


def _response(
    status: str,
    *,
    output: list[Any] | None = None,
    usage: Any | None = None,
    incomplete_reason: str | None = None,
    error: Any | None = None,
    request_id: str | None = None,
    status_code: int | None = None,
) -> SimpleNamespace:
    details = (
        SimpleNamespace(reason=incomplete_reason)
        if incomplete_reason is not None
        else None
    )
    return SimpleNamespace(
        status=status,
        output=output or [],
        usage=usage,
        incomplete_details=details,
        error=error,
        _request_id=request_id,
        status_code=status_code,
    )


def _completed_stream(*, output: list[Any] | None = None) -> FakeAsyncStream:
    return FakeAsyncStream([
        _event(
            "response.completed",
            response=_response("completed", output=output),
        )
    ])


def _bare_provider() -> DeepSeekProvider:
    provider = object.__new__(DeepSeekProvider)
    provider.event_bus = None
    provider.model = "deepseek-test"
    provider.reasoning_effort = "high"
    return provider


def _tool_events(arguments: str = '{"q":"weather"}') -> list[SimpleNamespace]:
    item = SimpleNamespace(
        type="function_call",
        id="output_1",
        call_id="call_1",
        name="lookup",
    )
    return [
        _event("response.output_item.added", item=item),
        _event(
            "response.function_call_arguments.delta",
            item_id="output_1",
            delta=arguments,
        ),
    ]


def test_deepseek_request_uses_responses_input_and_supported_runtime_fields() -> None:
    """请求应使用 Responses 形态且不残留 Chat Completions 专属字段。"""
    provider = _bare_provider()
    create = CapturingCreate(_completed_stream())
    provider._client = SimpleNamespace(responses=create)
    previous_output = [
        {"type": "reasoning", "id": "reasoning_1", "content": []},
        {
            "type": "function_call",
            "id": "output_1",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "_response_output": previous_output,
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Lookup weather",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    asyncio.run(
        provider._do_chat(
            messages,
            prompt=[
                {"role": "system", "content": "system one"},
                {"role": "developer", "content": "system two"},
            ],
            tools=tools,
            temperature=0.4,
            enable_thinking=True,
            call=_call(),
        )
    )

    request = create.requests[0]
    assert request["model"] == "deepseek-test"
    assert request["instructions"] == "system one\n\nsystem two"
    assert request["input"] == [
        {"role": "user", "content": "hello"},
        *previous_output,
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "sunny",
        },
    ]
    assert request["stream"] is True
    assert request["temperature"] == 0.4
    assert request["reasoning"] == {"effort": "high"}
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [{
        "type": "function",
        "name": "lookup",
        "description": "Lookup weather",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    }]
    assert not {
        "messages",
        "stream_options",
        "reasoning_effort",
        "extra_body",
        "prompt_cache_key",
        "previous_response_id",
        "conversation",
        "store",
    }.intersection(request)


def test_deepseek_request_omits_reasoning_when_thinking_is_disabled() -> None:
    """关闭思考时不应发送 reasoning，也不应伪造 disabled 扩展字段。"""
    provider = _bare_provider()
    create = CapturingCreate(_completed_stream())
    provider._client = SimpleNamespace(responses=create)

    asyncio.run(
        provider._do_chat(
            [{"role": "user", "content": "hello"}],
            enable_thinking=False,
            call=_call(),
        )
    )

    assert "reasoning" not in create.requests[0]
    assert "extra_body" not in create.requests[0]


def test_deepseek_request_converts_named_function_tool_choice() -> None:
    """指定 function 时应使用 Responses API 的扁平 tool choice 结构。"""
    provider = _bare_provider()
    create = CapturingCreate(_completed_stream())
    provider._client = SimpleNamespace(responses=create)
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    asyncio.run(
        provider._do_chat(
            [{"role": "user", "content": "hello"}],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "lookup"}},
            call=_call(),
        )
    )

    assert create.requests[0]["tool_choice"] == {
        "type": "function",
        "name": "lookup",
    }


def test_deepseek_stream_emits_reasoning_and_preserves_unknown_output_items() -> None:
    """已知增量应展示，未知事件和输出 item 应保持前向兼容。"""
    provider = _bare_provider()
    output = [
        DumpableItem({
            "type": "reasoning",
            "id": "reasoning_1",
            "content": [{"type": "reasoning_text", "text": "think"}],
        }),
        DumpableItem({"type": "future_output", "id": "future_1", "payload": "x"}),
    ]
    events = [
        _event("response.created"),
        _event("response.reasoning_text.delta", delta="think"),
        _event("response.future_capability.delta", delta="ignored"),
        _event("response.output_text.delta", delta="answer"),
        _event(
            "response.completed",
            response=_response("completed", output=output),
        ),
    ]
    call = _call()

    response = asyncio.run(
        provider._parse_stream(FakeAsyncStream(events), call=call)
    )

    assert response.content == "answer"
    assert response.finish_reason == "stop"
    assert call.partial_thinking == "think"
    assert response.assistant_message == {
        "role": "assistant",
        "content": "answer",
        "_response_output": [item.value for item in output],
    }


def test_deepseek_stream_parses_function_call_and_response_usage() -> None:
    """Function 调用和 Responses usage 应归一到公共响应。"""
    provider = _bare_provider()
    usage = SimpleNamespace(
        input_tokens=12,
        output_tokens=8,
        total_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=5),
        output_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    events = _tool_events() + [
        _event(
            "response.completed",
            response=_response("completed", usage=usage),
        )
    ]
    call = _call()

    response = asyncio.run(
        provider._parse_stream(FakeAsyncStream(events), call=call)
    )

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == {
        0: {"id": "call_1", "name": "lookup", "arguments": '{"q":"weather"}'}
    }
    assert response.token_usage == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": None,
    }
    assert call.tool_fragment_state == "complete"


def test_deepseek_function_arguments_done_can_complete_without_deltas() -> None:
    """只有 done 事件携带完整参数时也应构造合法工具调用。"""
    item = SimpleNamespace(
        type="function_call",
        id="output_1",
        call_id="call_1",
        name="lookup",
    )
    events = [
        _event("response.output_item.added", item=item),
        _event(
            "response.function_call_arguments.done",
            item_id="output_1",
            arguments='{"q":"weather"}',
        ),
        _event(
            "response.completed",
            response=_response("completed"),
        ),
    ]

    response = asyncio.run(
        _bare_provider()._parse_stream(FakeAsyncStream(events), call=_call())
    )

    assert response.tool_calls[0]["arguments"] == '{"q":"weather"}'


def test_deepseek_incomplete_max_output_tokens_returns_length() -> None:
    """输出上限终态应返回 length 并保留半截工具参数。"""
    events = _tool_events(arguments='{"q":') + [
        _event(
            "response.incomplete",
            response=_response(
                "incomplete",
                incomplete_reason="max_output_tokens",
            ),
        )
    ]
    call = _call()

    response = asyncio.run(
        _bare_provider()._parse_stream(FakeAsyncStream(events), call=call)
    )

    assert response.finish_reason == "length"
    assert response.tool_calls[0]["arguments"] == '{"q":'
    assert call.tool_fragment_state == "partial"


def test_deepseek_incomplete_requires_matching_response_status() -> None:
    """incomplete 事件不得携带其他响应状态。"""
    event = _event(
        "response.incomplete",
        response=_response(
            "completed",
            incomplete_reason="max_output_tokens",
        ),
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(
            _bare_provider()._parse_stream(FakeAsyncStream([event]), call=_call())
        )

    assert classify_llm_error(raised.value).kind is LLMErrorKind.RESPONSE_PROTOCOL


def test_deepseek_failed_event_preserves_safe_error_metadata() -> None:
    """失败终态应保留结构化错误信息供统一分类。"""
    event = _event(
        "response.failed",
        response=_response(
            "failed",
            error=SimpleNamespace(
                code="insufficient_system_resource",
                message="resource unavailable",
            ),
            request_id="req_deepseek",
            status_code=503,
        ),
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(
            _bare_provider()._parse_stream(FakeAsyncStream([event]), call=_call())
        )

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.SERVICE
    assert info.retryable is True
    assert info.provider_code == "insufficient_system_resource"
    assert info.request_id == "req_deepseek"


def test_deepseek_rejects_event_after_terminal() -> None:
    """合法终态后的任何事件仍应视为响应协议错误。"""
    events = [
        _event(
            "response.completed",
            response=_response("completed"),
        ),
        _event("response.future_event"),
    ]

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(
            _bare_provider()._parse_stream(FakeAsyncStream(events), call=_call())
        )

    assert classify_llm_error(raised.value).kind is LLMErrorKind.RESPONSE_PROTOCOL


def test_deepseek_tool_fragment_survives_stream_failure() -> None:
    """工具参数流中断时调用上下文应保留已到达残片。"""
    call = _call()

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(
            _bare_provider()._parse_stream(
                FakeAsyncStream(
                    _tool_events(arguments='{"q":'),
                    terminal_error=LLMStreamResponseError("stream interrupted"),
                ),
                call=call,
            )
        )

    assert call.tool_fragment_state == "partial"
    assert call.tool_fragments == {
        0: {"id": "call_1", "name": "lookup", "arguments": '{"q":'}
    }


def test_deepseek_clear_reasoning_keeps_other_response_output_items() -> None:
    """清理无工具轮次思考时不得删除未知或正文输出项。"""
    messages = [{
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "legacy",
        "_response_output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": []},
            {"type": "future_output", "payload": "x"},
        ],
    }]

    _bare_provider().clear_reasoning_content(messages)

    assert "reasoning_content" not in messages[0]
    assert messages[0]["_response_output"] == [
        {"type": "message", "content": []},
        {"type": "future_output", "payload": "x"},
    ]
