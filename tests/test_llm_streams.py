"""五个 LLM provider 的流式终态与工具调用协议测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest

from src.llm.anthropic import AnthropicProvider
from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.deepseek import DeepSeekProvider
from src.llm.errors import LLMErrorKind, LLMStreamResponseError, classify_llm_error
from src.llm.moonshot import MoonshotProvider
from src.llm.ollama import OllamaProvider
from src.llm.openai import OpenAIProvider


class FakeAsyncStream:
    """按顺序产生对象并可在末尾抛出异常的异步流。"""

    def __init__(
        self,
        events: list[Any],
        *,
        terminal_error: BaseException | None = None,
    ) -> None:
        """初始化异步流。

        Args:
            events: 依次产生的流对象。
            terminal_error: 对象耗尽后抛出的异常；缺省时正常 EOF。

        Returns:
            None。
        """
        self._events = list(events)
        self._terminal_error = terminal_error

    def __aiter__(self) -> AsyncIterator[Any]:
        """返回当前异步迭代器。

        Returns:
            当前异步迭代器。
        """
        return self

    async def __anext__(self) -> Any:
        """返回下一个对象或结束流。

        Returns:
            下一个流对象。

        Raises:
            BaseException: 配置了末尾异常且对象已耗尽时原样抛出。
            StopAsyncIteration: 对象正常耗尽时抛出。
        """
        if self._events:
            return self._events.pop(0)
        if self._terminal_error is not None:
            raise self._terminal_error
        raise StopAsyncIteration


class DumpableBlock:
    """模拟 SDK 内容块并记录 model_dump 调用。"""

    def __init__(self, data: dict[str, Any]) -> None:
        """保存待序列化字段。

        Args:
            data: model_dump 应返回的完整字段字典。

        Returns:
            None。
        """
        self._data = data
        self.dump_exclude_none: list[bool] = []
        for key, value in data.items():
            setattr(self, key, value)

    def model_dump(self, *, exclude_none: bool) -> dict[str, Any]:
        """返回模拟的 SDK 序列化结果。

        Args:
            exclude_none: 是否排除值为 None 的字段。

        Returns:
            与 SDK 内容块等价的字段字典。
        """
        self.dump_exclude_none.append(exclude_none)
        if exclude_none:
            return {key: value for key, value in self._data.items() if value is not None}
        return dict(self._data)


def _bare_provider(provider_type: type[LLMProvider]) -> Any:
    """构造不创建 SDK 客户端的 provider。

    Args:
        provider_type: 待构造的 provider 类型。

    Returns:
        仅配置流解析所需字段的 provider。
    """
    provider = object.__new__(provider_type)
    provider.event_bus = None
    provider.model = "stream-test"
    return provider


def _call() -> LLMCallContext:
    """构造单次测试调用上下文。

    Returns:
        首次调用的独立上下文。
    """
    return LLMCallContext(
        attempt=1,
        caller_agent_type="worker",
        caller_uuid="uuid-stream",
    )


def _response_event(
    event_type: str,
    *,
    response: Any | None = None,
    **fields: Any,
) -> SimpleNamespace:
    """构造 Responses API 流事件。

    Args:
        event_type: Responses API 事件类型。
        response: 可选的终态响应对象。
        fields: 附加到事件的字段。

    Returns:
        可由生产 parser 读取的事件对象。
    """
    return SimpleNamespace(type=event_type, response=response, **fields)


def _openai_response(
    status: str,
    *,
    incomplete_reason: str | None = None,
    error: Any | None = None,
    output: list[Any] | None = None,
    request_id: str | None = None,
    status_code: int | None = None,
) -> SimpleNamespace:
    """构造 Responses API 终态响应对象。

    Args:
        status: provider 响应状态。
        incomplete_reason: incomplete_details.reason。
        error: provider 错误对象。
        output: provider 最终输出项。
        request_id: provider 请求 ID。
        status_code: provider HTTP 状态码。

    Returns:
        可由生产 parser 读取的响应对象。
    """
    details = (
        SimpleNamespace(reason=incomplete_reason)
        if incomplete_reason is not None
        else None
    )
    return SimpleNamespace(
        status=status,
        incomplete_details=details,
        error=error,
        output=output or [],
        usage=None,
        _request_id=request_id,
        status_code=status_code,
    )


def _openai_tool_events(
    *,
    item_id: str = "output_1",
    call_id: str = "call_1",
    name: str = "lookup",
    arguments: str = '{"q":"weather"}',
) -> list[SimpleNamespace]:
    """构造一个完整 Responses API 工具调用事件序列。

    Args:
        item_id: Responses API 输出项 ID。
        call_id: 工具调用 ID。
        name: 工具名称。
        arguments: 工具参数 JSON 文本。

    Returns:
        工具项开始和参数增量事件列表。
    """
    item = SimpleNamespace(
        type="function_call",
        id=item_id,
        call_id=call_id,
        name=name,
    )
    return [
        _response_event("response.output_item.added", item=item),
        _response_event(
            "response.function_call_arguments.delta",
            item_id=item_id,
            delta=arguments,
        ),
    ]


def test_openai_completed_is_the_only_normal_terminal() -> None:
    """Responses completed 终态应返回正常 stop 响应。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)
    call = _call()
    events = [
        _response_event("response.output_text.delta", delta="hello"),
        _response_event(
            "response.completed",
            response=_openai_response("completed"),
        ),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=call))

    assert response.content == "hello"
    assert response.finish_reason == "stop"
    assert response.tool_calls == {}


def test_openai_completed_validates_and_returns_tool_calls() -> None:
    """Responses completed 携带合法工具时应返回 tool_calls。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)
    call = _call()
    events = _openai_tool_events() + [
        _response_event(
            "response.completed",
            response=_openai_response("completed"),
        )
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=call))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == {
        0: {"id": "call_1", "name": "lookup", "arguments": '{"q":"weather"}'}
    }
    assert call.tool_fragment_state == "complete"


def test_openai_incomplete_max_output_tokens_returns_length_before_tool_validation() -> None:
    """Responses 输出上限应直接返回 length 并保留半截工具参数。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)
    call = _call()
    events = _openai_tool_events(arguments='{"q":') + [
        _response_event(
            "response.incomplete",
            response=_openai_response(
                "incomplete",
                incomplete_reason="max_output_tokens",
            ),
        )
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=call))

    assert response.finish_reason == "length"
    assert response.tool_calls[0]["arguments"] == '{"q":'
    assert call.tool_fragment_state == "partial"


def test_openai_incomplete_content_filter_is_non_retryable_policy_error() -> None:
    """Responses 内容过滤终态应归为不可重试内容政策错误。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)
    event = _response_event(
        "response.incomplete",
        response=_openai_response(
            "incomplete",
            incomplete_reason="content_filter",
            request_id="req_policy",
        ),
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream([event]), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.CONTENT_POLICY
    assert info.retryable is False
    assert info.provider_code == "content_filter"
    assert info.request_id == "req_policy"


@pytest.mark.parametrize(
    ("event_type", "refusal_field"),
    [
        ("response.refusal.delta", {"delta": "unsafe refusal delta"}),
        ("response.refusal.done", {"refusal": "unsafe refusal body"}),
    ],
)
def test_openai_refusal_events_are_non_retryable_policy_errors(
    event_type: str,
    refusal_field: dict[str, str],
) -> None:
    """Responses refusal 事件不得被接受为空 stop 响应。

    Args:
        event_type: refusal SSE 事件类型。
        refusal_field: 事件携带的拒绝正文字段。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)
    event = _response_event(event_type, **refusal_field)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream([event]), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.CONTENT_POLICY
    assert info.retryable is False
    assert info.provider_code == "refusal"
    assert all(value not in info.message for value in refusal_field.values())


@pytest.mark.parametrize(
    "output_item",
    [
        DumpableBlock({
            "type": "message",
            "content": [{"type": "refusal", "refusal": "unsafe nested refusal"}],
        }),
        {"type": "refusal", "refusal": "unsafe direct refusal"},
    ],
)
def test_openai_terminal_output_refusal_is_non_retryable_policy_error(
    output_item: Any,
) -> None:
    """completed.output 中的 refusal block 不得作为普通输出成功返回。

    Args:
        output_item: 含典型嵌套或直接 refusal block 的最终输出项。

    Returns:
        None。
    """
    terminal = _openai_response("completed", output=[output_item])
    event = _response_event("response.completed", response=terminal)
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream([event]), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.CONTENT_POLICY
    assert info.retryable is False
    assert info.provider_code == "refusal"
    assert "unsafe" not in info.message


@pytest.mark.parametrize(
    ("event", "expected_code", "expected_request_id"),
    [
        (
            _response_event(
                "response.failed",
                response=_openai_response(
                    "failed",
                    error=SimpleNamespace(
                        code="server_error",
                        message="failed with api_key=sk-provider-secret",
                    ),
                    request_id="req_failed",
                    status_code=500,
                ),
            ),
            "server_error",
            "req_failed",
        ),
        (
            _response_event(
                "error",
                error=SimpleNamespace(
                    code="rate_limit_exceeded",
                    message="rate limited",
                ),
                request_id="req_error",
                status_code=429,
            ),
            "rate_limit_exceeded",
            "req_error",
        ),
    ],
)
def test_openai_failed_and_error_events_preserve_safe_metadata(
    event: SimpleNamespace,
    expected_code: str,
    expected_request_id: str,
) -> None:
    """Responses failed/error 事件应交给统一分类器提取有限元数据。

    Args:
        event: 待解析的错误终态事件。
        expected_code: 预期供应商错误码。
        expected_request_id: 预期请求 ID。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream([event]), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.provider_code == expected_code
    assert info.request_id == expected_request_id
    assert info.retryable is True
    assert "sk-provider-secret" not in info.message


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        ("invalid_prompt", LLMErrorKind.BAD_REQUEST),
        ("image_content_policy_violation", LLMErrorKind.CONTENT_POLICY),
    ],
)
def test_openai_permanent_stream_error_codes_are_not_protocol_retries(
    code: str,
    expected_kind: LLMErrorKind,
) -> None:
    """Responses 永久错误码应归到语义类别且禁止重试。

    Args:
        code: OpenAI Responses 永久错误码。
        expected_kind: 预期统一错误类别。

    Returns:
        None。
    """
    event = _response_event(
        "error",
        error=SimpleNamespace(code=code, message="permanent response error"),
        request_id="req_permanent",
        status_code=400,
    )
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream([event]), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is expected_kind
    assert info.provider_code == code
    assert info.retryable is False


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        ("invalid_image", LLMErrorKind.BAD_REQUEST),
        ("invalid_image_format", LLMErrorKind.BAD_REQUEST),
        ("invalid_base64_image", LLMErrorKind.BAD_REQUEST),
        ("invalid_image_url", LLMErrorKind.BAD_REQUEST),
        ("image_too_large", LLMErrorKind.PAYLOAD_TOO_LARGE),
        ("image_too_small", LLMErrorKind.BAD_REQUEST),
        ("image_parse_error", LLMErrorKind.BAD_REQUEST),
        ("invalid_image_mode", LLMErrorKind.BAD_REQUEST),
        ("image_file_too_large", LLMErrorKind.PAYLOAD_TOO_LARGE),
        ("unsupported_image_media_type", LLMErrorKind.BAD_REQUEST),
        ("empty_image_file", LLMErrorKind.BAD_REQUEST),
        ("image_file_not_found", LLMErrorKind.NOT_FOUND),
    ],
)
def test_openai_permanent_image_input_codes_are_not_retryable(
    code: str,
    expected_kind: LLMErrorKind,
) -> None:
    """OpenAI 永久图像输入错误码应映射为明确的非重试类别。

    Args:
        code: OpenAI Responses 图像输入错误码。
        expected_kind: 预期统一错误类别。

    Returns:
        None。
    """
    error = LLMStreamResponseError(
        "invalid image input",
        code=code,
        status_code=400,
        request_id="req_image",
    )

    info = classify_llm_error(error)

    assert info.kind is expected_kind
    assert info.provider_code == code
    assert info.retryable is False


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_response_event("response.output_text.delta", delta="partial")],
        [_response_event("response.cancelled")],
        [
            _response_event(
                "response.completed",
                response=_openai_response("mystery"),
            )
        ],
    ],
)
def test_openai_missing_or_unknown_terminal_is_retryable_protocol_error(
    events: list[SimpleNamespace],
) -> None:
    """Responses 缺少合法终态或出现未知终态时应报可重试协议错误。

    Args:
        events: 待解析的流事件。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize(
    "events",
    [
        [
            _response_event(
                "response.completed",
                response=_openai_response("completed"),
            ),
            _response_event(
                "response.completed",
                response=_openai_response("completed"),
            ),
        ],
        [
            _response_event(
                "response.incomplete",
                response=_openai_response(
                    "incomplete",
                    incomplete_reason="max_output_tokens",
                ),
            ),
            _response_event(
                "response.completed",
                response=_openai_response("completed"),
            ),
        ],
        [
            _response_event(
                "response.completed",
                response=_openai_response("completed"),
            ),
            _response_event("response.output_text.delta", delta="late delta"),
        ],
    ],
)
def test_openai_rejects_every_event_after_terminal(
    events: list[SimpleNamespace],
) -> None:
    """Responses API 任一成功或截断终态后不得再接受事件。

    Args:
        events: 含重复终态或终态后 delta 的事件序列。

    Returns:
        None。
    """
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_openai_rejects_arguments_delta_for_unknown_output_item() -> None:
    """Responses API 未注册 item_id 的工具参数增量应报协议错误。

    Returns:
        None。
    """
    events = [
        _response_event(
            "response.function_call_arguments.delta",
            item_id="missing_output",
            delta="{}",
        ),
        _response_event(
            "response.completed",
            response=_openai_response("completed"),
        ),
    ]
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_openai_allows_lifecycle_events_before_terminal() -> None:
    """Responses API 合法 lifecycle 事件应可出现在终态之前。

    Returns:
        None。
    """
    events = [
        _response_event("response.created"),
        _response_event("response.in_progress"),
        _response_event("response.output_text.delta", delta="done"),
        _response_event(
            "response.completed",
            response=_openai_response("completed"),
        ),
    ]
    provider = _bare_provider(OpenAIProvider)

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=_call()))

    assert response.content == "done"
    assert response.finish_reason == "stop"


CHAT_PROVIDERS = [DeepSeekProvider, MoonshotProvider, OllamaProvider]


def _chat_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    tool_call: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """构造 OpenAI 兼容 Chat Completions 流数据块。

    Args:
        content: 正文增量。
        finish_reason: 终态原因。
        tool_call: 单个工具片段的 id、name、arguments 字段。

    Returns:
        可由三个生产 parser 读取的数据块。
    """
    tool_chunks: list[SimpleNamespace] = []
    if tool_call is not None:
        function = SimpleNamespace(
            name=tool_call.get("name", ""),
            arguments=tool_call.get("arguments", ""),
        )
        tool_chunks.append(
            SimpleNamespace(
                index=tool_call.get("index", 0),
                id=tool_call.get("id", ""),
                function=function,
            )
        )
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_chunks,
        reasoning=None,
        reasoning_content=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(usage=None, choices=[choice])


def _chat_usage_chunk() -> SimpleNamespace:
    """构造仅包含 token 用量的 Chat Completions 尾块。

    Returns:
        不含 choice 的 usage-only 流数据块。
    """
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        prompt_tokens_details=None,
    )
    return SimpleNamespace(usage=usage, choices=[])


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [_chat_chunk(content="partial")],
        [_chat_chunk(finish_reason="not_a_finish_reason")],
    ],
)
def test_chat_completion_requires_valid_finish_reason(
    provider_type: type[LLMProvider],
    chunks: list[SimpleNamespace],
) -> None:
    """三个兼容 provider 缺终态或终态非法时应报可重试协议错误。

    Args:
        provider_type: 待验证 provider 类型。
        chunks: 待解析的兼容流数据块。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_allows_usage_only_chunk_after_terminal(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 的业务终态后应接受 usage-only 尾块。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(content="done"),
        _chat_chunk(finish_reason="stop"),
        _chat_usage_chunk(),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    assert response.content == "done"
    assert response.finish_reason == "stop"
    assert response.token_usage is not None
    assert response.token_usage["total_tokens"] == 18


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
@pytest.mark.parametrize(
    "post_terminal_chunk",
    [
        pytest.param(_chat_chunk(), id="choice"),
        pytest.param(_chat_chunk(content="leaked"), id="delta"),
        pytest.param(_chat_chunk(finish_reason="length"), id="finish-reason"),
    ],
)
def test_chat_completion_rejects_choice_after_terminal_without_recording_delta(
    provider_type: type[LLMProvider],
    post_terminal_chunk: SimpleNamespace,
) -> None:
    """业务终态后的 choice 应报协议错且不得记录其正文。

    Args:
        provider_type: 待验证 provider 类型。
        post_terminal_chunk: 终态后到达的非法 choice 数据块。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    call = _call()
    chunks = [
        _chat_chunk(content="kept"),
        _chat_chunk(finish_reason="stop"),
        post_terminal_chunk,
    ]

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=call))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True
    assert call.partial_output == "kept"


@pytest.mark.parametrize(
    ("provider_type", "finish_reason", "expected_kind", "expected_code"),
    [
        pytest.param(
            provider_type,
            "content_filter",
            LLMErrorKind.CONTENT_POLICY,
            "content_filter",
            id=f"{provider_type.__name__}-content-filter",
        )
        for provider_type in CHAT_PROVIDERS
    ]
    + [
        pytest.param(
            provider_type,
            "unknown_terminal",
            LLMErrorKind.RESPONSE_PROTOCOL,
            "invalid_response",
            id=f"{provider_type.__name__}-unknown",
        )
        for provider_type in CHAT_PROVIDERS
    ]
    + [
        pytest.param(
            DeepSeekProvider,
            "insufficient_system_resource",
            LLMErrorKind.SERVICE,
            "insufficient_system_resource",
            id="DeepSeekProvider-insufficient-system-resource",
        )
    ],
)
def test_chat_completion_preserves_first_special_terminal(
    provider_type: type[LLMProvider],
    finish_reason: str,
    expected_kind: LLMErrorKind,
    expected_code: str,
) -> None:
    """首个特殊终态不得被后置正常终态覆盖。

    Args:
        provider_type: 待验证 provider 类型。
        finish_reason: 首个供应商终态原因。
        expected_kind: 首个终态应映射的错误类别。
        expected_code: 首个终态应保留的供应商 code。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(finish_reason=finish_reason),
        _chat_chunk(finish_reason="stop"),
    ]

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is expected_kind
    assert info.provider_code == expected_code


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_length_returns_partial_tool_call_without_json_validation(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 的 length 应优先返回半截工具响应。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    call = _call()
    chunks = [
        _chat_chunk(
            tool_call={"id": "call_1", "name": "lookup", "arguments": '{"q":'},
        ),
        _chat_chunk(finish_reason="length"),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=call))

    assert response.finish_reason == "length"
    assert response.tool_calls[0]["arguments"] == '{"q":'
    assert call.tool_fragment_state == "partial"


class LengthSuccessProvider(LLMProvider):
    """返回带半截工具调用的 length 响应测试 provider。"""

    captured_call: LLMCallContext | None

    def __post_init__(self) -> None:
        """初始化基类状态与捕获字段。

        Returns:
            None。
        """
        super().__post_init__()
        self.captured_call = None

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[Any] | None = None,
    ) -> int:
        """返回测试固定 token 数。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词列表。
            tools: 可选工具 schema 列表。

        Returns:
            固定值 0。
        """
        return 0

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[Any] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """记录半截工具片段并返回 length 响应。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词列表。
            tools: 可选工具 schema 列表。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位。
            call: 当前独立调用上下文。

        Returns:
            带半截工具参数的 length 响应。
        """
        del reasoning_effort_override
        self.captured_call = call
        call.record_tool_fragment(
            0,
            call_id="call_1",
            name="lookup",
            arguments='{"q":',
        )
        tool_calls = {
            0: {"id": "call_1", "name": "lookup", "arguments": '{"q":'}
        }
        return LLMResponse(
            content="",
            tool_calls=tool_calls,
            finish_reason="length",
        )


def test_provider_chat_keeps_length_tool_fragment_partial() -> None:
    """基类成功路径不得把 length 的半截工具片段标记为完整。

    Returns:
        None。
    """
    provider = LengthSuccessProvider(
        api_key="",
        base_url="",
        model="length-test",
        event_bus=None,
        max_attempts=1,
    )

    response = asyncio.run(provider.chat([{"role": "user", "content": "continue"}]))

    assert response.finish_reason == "length"
    assert provider.captured_call is not None
    assert provider.captured_call.tool_fragment_state == "partial"


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_content_filter_is_non_retryable(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 的 content_filter 应归内容政策错误。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(
            provider._parse_stream(
                FakeAsyncStream([_chat_chunk(finish_reason="content_filter")]),
                call=_call(),
            )
        )

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.CONTENT_POLICY
    assert info.retryable is False


def test_deepseek_insufficient_system_resource_is_retryable_service_error() -> None:
    """DeepSeek 合法资源不足终态应归为可重试服务错误。

    Returns:
        None。
    """
    provider = _bare_provider(DeepSeekProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(
            provider._parse_stream(
                FakeAsyncStream(
                    [_chat_chunk(finish_reason="insufficient_system_resource")]
                ),
                call=_call(),
            )
        )

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.SERVICE
    assert info.retryable is True
    assert info.provider_code == "insufficient_system_resource"


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_accepts_normal_stop_without_tools(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 的正常 stop 无工具响应应合法。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(content="done"),
        _chat_chunk(finish_reason="stop"),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    assert response.content == "done"
    assert response.finish_reason == "stop"
    assert response.tool_calls == {}


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_accepts_valid_tool_call(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 应接受字段完整且参数为 JSON object 的工具调用。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(
            tool_call={
                "id": "call_1",
                "name": "lookup",
                "arguments": '{"q":"weather"}',
            }
        ),
        _chat_chunk(finish_reason="tool_calls"),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0]["name"] == "lookup"


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
@pytest.mark.parametrize(
    ("finish_reason", "tool_call"),
    [
        ("tool_calls", None),
        (
            "stop",
            {"id": "call_1", "name": "lookup", "arguments": "{}"},
        ),
    ],
)
def test_chat_completion_rejects_finish_reason_tool_mismatch(
    provider_type: type[LLMProvider],
    finish_reason: str,
    tool_call: dict[str, str] | None,
) -> None:
    """三个兼容 provider 应拒绝工具终态与工具调用存在性不一致。

    Args:
        provider_type: 待验证 provider 类型。
        finish_reason: 待解析的终态原因。
        tool_call: 可选工具调用片段。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(tool_call=tool_call),
        _chat_chunk(finish_reason=finish_reason),
    ]

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
def test_chat_completion_rejects_duplicate_tool_call_ids(
    provider_type: type[LLMProvider],
) -> None:
    """三个兼容 provider 应拒绝跨 index 重复的工具调用 ID。

    Args:
        provider_type: 待验证 provider 类型。

    Returns:
        None。
    """
    chunks = [
        _chat_chunk(
            tool_call={
                "index": 0,
                "id": "duplicate_call",
                "name": "first_tool",
                "arguments": "{}",
            }
        ),
        _chat_chunk(
            tool_call={
                "index": 1,
                "id": "duplicate_call",
                "name": "second_tool",
                "arguments": "{}",
            }
        ),
        _chat_chunk(finish_reason="tool_calls"),
    ]
    provider = _bare_provider(provider_type)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_openai_rejects_duplicate_tool_call_ids() -> None:
    """Responses API 应拒绝跨输出项重复的工具调用 ID。

    Returns:
        None。
    """
    events = (
        _openai_tool_events(item_id="output_1", call_id="duplicate_call")
        + _openai_tool_events(item_id="output_2", call_id="duplicate_call")
        + [
            _response_event(
                "response.completed",
                response=_openai_response("completed"),
            )
        ]
    )
    provider = _bare_provider(OpenAIProvider)

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(events), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_ollama_emits_content_delta_before_stream_failure() -> None:
    """Ollama 正文到达时应立即展示并在断流后保留 partial_output。

    Returns:
        None。
    """
    provider = _bare_provider(OllamaProvider)
    call = _call()
    stream = FakeAsyncStream(
        [_chat_chunk(content="visible partial")],
        terminal_error=LLMStreamResponseError("stream interrupted"),
    )

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(provider._parse_stream(stream, call=call))

    assert call.partial_output == "visible partial"
    assert call.response_displayed is True


def test_ollama_success_does_not_emit_content_twice() -> None:
    """Ollama 正常结束后不得重复记录已即时展示的正文。

    Returns:
        None。
    """
    provider = _bare_provider(OllamaProvider)
    call = _call()
    chunks = [
        _chat_chunk(content="hello "),
        _chat_chunk(content="world"),
        _chat_chunk(finish_reason="stop"),
    ]

    response = asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=call))

    assert response.content == "hello world"
    assert call.partial_output == "hello world"


@pytest.mark.parametrize("provider_type", CHAT_PROVIDERS)
@pytest.mark.parametrize(
    "tool_call",
    [
        {"id": "", "name": "lookup", "arguments": "{}"},
        {"id": "call_1", "name": "", "arguments": "{}"},
        {"id": "call_1", "name": "lookup", "arguments": "{"},
        {"id": "call_1", "name": "lookup", "arguments": "[]"},
    ],
)
def test_chat_completion_rejects_malformed_tool_call(
    provider_type: type[LLMProvider],
    tool_call: dict[str, str],
) -> None:
    """三个兼容 provider 应拒绝字段或 JSON object 参数畸形的工具调用。

    Args:
        provider_type: 待验证 provider 类型。
        tool_call: 待解析的畸形工具调用。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    chunks = [
        _chat_chunk(tool_call=tool_call),
        _chat_chunk(finish_reason="tool_calls"),
    ]

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._parse_stream(FakeAsyncStream(chunks), call=_call()))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize(
    "provider_type",
    [OpenAIProvider, DeepSeekProvider, MoonshotProvider, OllamaProvider],
)
@pytest.mark.parametrize(
    "terminal_error",
    [
        LLMStreamResponseError("stream interrupted"),
        EOFError("unexpected EOF"),
    ],
)
def test_additional_provider_keeps_tool_fragment_on_stream_failure(
    provider_type: type[LLMProvider],
    terminal_error: BaseException,
) -> None:
    """四个 OpenAI 风格 provider 工具流断裂时应保留到达的残片。

    Args:
        provider_type: 待验证 provider 类型。
        terminal_error: 流对象耗尽后抛出的中断异常。

    Returns:
        None。
    """
    provider = _bare_provider(provider_type)
    call = _call()
    if provider_type is OpenAIProvider:
        events: list[Any] = _openai_tool_events(arguments='{"q":')
    else:
        events = [
            _chat_chunk(
                tool_call={"id": "call_1", "name": "lookup", "arguments": '{"q":'},
            )
        ]

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(
            provider._parse_stream(
                FakeAsyncStream(events, terminal_error=terminal_error),
                call=call,
            )
        )

    assert call.tool_fragment_state == "partial"
    assert call.tool_fragments == {
        0: {"id": "call_1", "name": "lookup", "arguments": '{"q":'}
    }


class FakeAnthropicStream:
    """模拟 Anthropic SDK 的异步消息流上下文。"""

    def __init__(
        self,
        events: list[Any],
        *,
        final_message: Any | None,
        terminal_error: BaseException | None = None,
    ) -> None:
        """初始化 Anthropic 流。

        Args:
            events: 依次产生的 SSE 事件。
            final_message: get_final_message 返回值。
            terminal_error: 事件耗尽后抛出的异常；缺省时正常 EOF。

        Returns:
            None。
        """
        self._stream = FakeAsyncStream(events, terminal_error=terminal_error)
        self._final_message = final_message

    async def __aenter__(self) -> FakeAnthropicStream:
        """进入异步流上下文。

        Returns:
            当前流对象。
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any | None,
    ) -> bool:
        """退出异步流上下文且不吞掉异常。

        Args:
            exc_type: 异常类型。
            exc: 异常实例。
            traceback: 异常堆栈。

        Returns:
            固定为 False。
        """
        return False

    def __aiter__(self) -> AsyncIterator[Any]:
        """返回事件异步迭代器。

        Returns:
            当前流的事件迭代器。
        """
        return self._stream

    async def get_final_message(self) -> Any | None:
        """返回配置的最终消息。

        Returns:
            配置的最终消息或 None。
        """
        return self._final_message


class FakeAnthropicMessages:
    """提供 Anthropic messages.stream 测试接口。"""

    def __init__(self, stream: FakeAnthropicStream) -> None:
        """保存待返回的流。

        Args:
            stream: 待返回的 Anthropic 流。

        Returns:
            None。
        """
        self._stream = stream

    def stream(self, **kwargs: Any) -> FakeAnthropicStream:
        """返回预设 Anthropic 流。

        Args:
            kwargs: 生产 provider 下发的参数，本测试不使用。

        Returns:
            预设 Anthropic 流。
        """
        return self._stream


def _anthropic_provider(stream: FakeAnthropicStream) -> AnthropicProvider:
    """构造使用预设流的 Anthropic provider。

    Args:
        stream: messages.stream 应返回的流。

    Returns:
        不连接网络的 Anthropic provider。
    """
    provider = _bare_provider(AnthropicProvider)
    provider._client = SimpleNamespace(messages=FakeAnthropicMessages(stream))
    return provider


def _anthropic_message(
    stop_reason: str | None,
    *,
    content: list[Any] | None = None,
) -> SimpleNamespace:
    """构造 Anthropic 最终消息。

    Args:
        stop_reason: Anthropic 终止原因。
        content: 最终内容块列表。

    Returns:
        可由生产 provider 构造统一响应的消息。
    """
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content or [],
        usage=None,
    )


def _anthropic_message_stop() -> SimpleNamespace:
    """构造 Anthropic message_stop SSE 终态事件。

    Returns:
        可由生产 parser 识别的 message_stop 事件。
    """
    return SimpleNamespace(type="message_stop")


@pytest.mark.parametrize(
    ("stop_reason", "expected_finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("pause_turn", "pause_turn"),
    ],
)
def test_anthropic_maps_valid_stop_reasons(
    stop_reason: str,
    expected_finish: str,
) -> None:
    """Anthropic 合法停止原因应映射为统一终态。

    Args:
        stop_reason: Anthropic 原生停止原因。
        expected_finish: 预期统一终态。

    Returns:
        None。
    """
    content = None
    if stop_reason == "pause_turn":
        content = [SimpleNamespace(
            type="server_tool_use",
            id="srv_1",
            name="web_search",
            input={"query": "weather"},
        )]
    final = _anthropic_message(stop_reason, content=content)
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    response = asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    assert response.finish_reason == expected_finish


def test_anthropic_rejects_pause_turn_without_content_blocks() -> None:
    """Anthropic pause_turn 缺少原始内容块时应报协议错误。

    Returns:
        None。
    """
    final = _anthropic_message("pause_turn", content=[])
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_anthropic_accepts_valid_tool_use() -> None:
    """Anthropic 应接受 ID、名称和 object input 均合法的工具块。

    Returns:
        None。
    """
    tool = SimpleNamespace(
        type="tool_use",
        id="toolu_1",
        name="lookup",
        input={"q": "weather"},
    )
    final = _anthropic_message("tool_use", content=[tool])
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    response = asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0]["arguments"] == '{"q": "weather"}'


def test_anthropic_dumps_all_blocks_and_only_extracts_client_tool_use() -> None:
    """Anthropic 应完整保存所有 SDK block 且只提取客户端 tool_use。

    Returns:
        None。
    """
    blocks = [
        DumpableBlock({
            "type": "tool_use",
            "id": "toolu_client",
            "name": "lookup",
            "input": {"q": "weather"},
            "caller": {"type": "direct"},
            "optional": None,
        }),
        DumpableBlock({
            "type": "server_tool_use",
            "id": "srv_1",
            "name": "web_search",
            "input": {"query": "weather"},
            "caller": {"type": "server_tool"},
        }),
        DumpableBlock({
            "type": "web_search_tool_result",
            "tool_use_id": "srv_1",
            "content": [{"type": "web_search_result", "url": "https://example.test"}],
        }),
        DumpableBlock({
            "type": "web_fetch_tool_result",
            "tool_use_id": "srv_2",
            "content": {"type": "web_fetch_result", "url": "https://example.test/doc"},
        }),
        DumpableBlock({
            "type": "future_server_result",
            "future_field": {"nested": [1, 2, 3]},
        }),
    ]
    final = _anthropic_message("tool_use", content=blocks)
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    response = asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    assert response.tool_calls == {
        0: {
            "id": "toolu_client",
            "name": "lookup",
            "arguments": '{"q": "weather"}',
        }
    }
    assert response.assistant_message["_anthropic_content"] == [
        {key: value for key, value in block._data.items() if value is not None}
        for block in blocks
    ]
    assert all(block.dump_exclude_none == [True] for block in blocks)


@pytest.mark.parametrize("final", [None, _anthropic_message(None), _anthropic_message("mystery")])
def test_anthropic_requires_final_message_and_valid_stop_reason(final: Any | None) -> None:
    """Anthropic 缺 final 或 stop_reason 非法时应报可重试协议错误。

    Args:
        final: get_final_message 返回值。

    Returns:
        None。
    """
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize(
    ("stop_reason", "expected_kind"),
    [
        ("model_context_window_exceeded", LLMErrorKind.CONTEXT_LIMIT),
        ("refusal", LLMErrorKind.CONTENT_POLICY),
        ("content_filter", LLMErrorKind.CONTENT_POLICY),
    ],
)
def test_anthropic_semantic_stop_reasons_are_not_retryable(
    stop_reason: str,
    expected_kind: LLMErrorKind,
) -> None:
    """Anthropic 上下文和拒绝终态应归到对应不可重试语义错误。

    Args:
        stop_reason: Anthropic 原生停止原因。
        expected_kind: 预期统一错误分类。

    Returns:
        None。
    """
    provider = _anthropic_provider(
        FakeAnthropicStream(
            [_anthropic_message_stop()],
            final_message=_anthropic_message(stop_reason),
        )
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is expected_kind
    assert info.retryable is False


@pytest.mark.parametrize(
    "tool",
    [
        SimpleNamespace(type="tool_use", id="", name="lookup", input={}),
        SimpleNamespace(type="tool_use", id="toolu_1", name="", input={}),
        SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input=[]),
    ],
)
def test_anthropic_rejects_malformed_tool_use(tool: SimpleNamespace) -> None:
    """Anthropic 应拒绝 ID、名称或 object input 非法的工具块。

    Args:
        tool: 待解析的畸形工具块。

    Returns:
        None。
    """
    final = _anthropic_message("tool_use", content=[tool])
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_anthropic_rejects_duplicate_tool_call_ids() -> None:
    """Anthropic 应拒绝多个 tool_use 块复用同一调用 ID。

    Returns:
        None。
    """
    tools = [
        SimpleNamespace(type="tool_use", id="duplicate_tool", name="first", input={}),
        SimpleNamespace(type="tool_use", id="duplicate_tool", name="second", input={}),
    ]
    final = _anthropic_message("tool_use", content=tools)
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize(
    ("stop_reason", "content"),
    [
        (
            "end_turn",
            [SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={})],
        ),
        (
            "pause_turn",
            [SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={})],
        ),
        ("tool_use", []),
    ],
)
def test_anthropic_rejects_stop_reason_tool_mismatch(
    stop_reason: str,
    content: list[SimpleNamespace],
) -> None:
    """Anthropic 应拒绝 stop_reason 与 tool_use 存在性不一致。

    Args:
        stop_reason: Anthropic 原生停止原因。
        content: 最终消息内容块。

    Returns:
        None。
    """
    final = _anthropic_message(stop_reason, content=content)
    provider = _anthropic_provider(
        FakeAnthropicStream([_anthropic_message_stop()], final_message=final)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


_ANTHROPIC_CACHEABLE_BLOCK_TYPES = [
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
]


@pytest.mark.parametrize("block_type", _ANTHROPIC_CACHEABLE_BLOCK_TYPES)
def test_anthropic_cache_control_accepts_sdk_whitelist(block_type: str) -> None:
    """Anthropic 当前允许 cache_control 的 MessageParam block 应可作为断点。

    Args:
        block_type: SDK MessageParam 中显式支持 cache_control 的 block 类型。

    Returns:
        None。
    """
    messages = [{"role": "user", "content": [{"type": block_type}]}]
    provider = _bare_provider(AnthropicProvider)

    provider._apply_cache_control(messages)

    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_cache_control_skips_disallowed_trailing_blocks() -> None:
    """Anthropic 缓存断点应向前选择最后一个允许的 block。

    Returns:
        None。
    """
    messages = [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "cacheable"},
            {"type": "thinking", "thinking": "private", "signature": "sig"},
            {"type": "redacted_thinking", "data": "encrypted"},
            {"type": "future_block", "payload": "unknown"},
        ],
    }]
    provider = _bare_provider(AnthropicProvider)

    provider._apply_cache_control(messages)

    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in block for block in messages[0]["content"][1:])


def test_anthropic_cache_control_ignores_all_disallowed_blocks() -> None:
    """Anthropic 消息全为禁用或未知 block 时不得添加缓存断点。

    Returns:
        None。
    """
    content = [
        {"type": "thinking", "thinking": "private", "signature": "sig"},
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "future_block", "payload": "unknown"},
    ]
    messages = [{"role": "assistant", "content": content}]
    provider = _bare_provider(AnthropicProvider)

    provider._apply_cache_control(messages)

    assert all("cache_control" not in block for block in content)


def test_anthropic_cache_control_does_not_mutate_history_carrier() -> None:
    """回传 Anthropic 原始 carrier 时缓存注入不得反向修改历史消息。

    Returns:
        None。
    """
    carrier = [
        {"type": "text", "text": "persisted"},
        {"type": "thinking", "thinking": "private", "signature": "sig"},
    ]
    provider = _bare_provider(AnthropicProvider)

    _, converted = provider._convert_messages(
        [{"role": "assistant", "content": None, "_anthropic_content": carrier}],
        None,
    )
    provider._apply_cache_control(converted)

    assert carrier == [
        {"type": "text", "text": "persisted"},
        {"type": "thinking", "thinking": "private", "signature": "sig"},
    ]
    assert converted[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in converted[0]["content"][1]


def test_anthropic_requires_message_stop_even_with_valid_final_message() -> None:
    """Anthropic 缺少 message_stop 时不得接受 stop_reason 合法的 final message。

    Returns:
        None。
    """
    final = _anthropic_message("end_turn")
    provider = _anthropic_provider(FakeAnthropicStream([], final_message=final))

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_anthropic_rejects_event_after_message_stop() -> None:
    """Anthropic message_stop 后出现任何 SSE 事件都应报协议错误。

    Returns:
        None。
    """
    text_delta = SimpleNamespace(type="text_delta", text="late")
    events = [
        _anthropic_message_stop(),
        SimpleNamespace(type="content_block_delta", index=0, delta=text_delta),
    ]
    provider = _anthropic_provider(
        FakeAnthropicStream(events, final_message=_anthropic_message("end_turn"))
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_anthropic_sse_error_event_uses_provider_metadata() -> None:
    """Anthropic SSE error 事件应保留有限供应商元数据供分类器读取。

    Returns:
        None。
    """
    error = SimpleNamespace(type="overloaded_error", message="provider overloaded")
    event = SimpleNamespace(type="error", error=error, request_id="req_anthropic")
    provider = _anthropic_provider(
        FakeAnthropicStream([event], final_message=None)
    )

    with pytest.raises(LLMStreamResponseError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.SERVICE
    assert info.provider_code == "overloaded_error"
    assert info.request_id == "req_anthropic"
    assert info.retryable is True


def test_anthropic_sdk_wrapper_preserves_transport_cause_classification() -> None:
    """Anthropic SDK 包装异常应沿 cause 识别底层传输失败。

    Returns:
        None。
    """
    request = httpx.Request("POST", "https://example.test/v1/messages")
    transport = httpx.ReadError("connection reset", request=request)
    try:
        try:
            raise transport
        except httpx.ReadError as cause:
            raise RuntimeError("SDK stream wrapper") from cause
    except RuntimeError as wrapper:
        terminal_error = wrapper

    provider = _anthropic_provider(
        FakeAnthropicStream([], final_message=None, terminal_error=terminal_error)
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(provider._stream_chat(call=_call(), model="stream-test"))

    info = classify_llm_error(raised.value)
    assert info.kind is LLMErrorKind.NETWORK
    assert info.retryable is True


def test_anthropic_server_tool_json_is_not_a_client_tool_fragment() -> None:
    """Anthropic server_tool_use 参数增量不得记录为客户端工具残片。

    Returns:
        None。
    """
    server_block = SimpleNamespace(
        type="server_tool_use",
        id="srv_1",
        name="web_search",
    )
    input_delta = SimpleNamespace(
        type="input_json_delta",
        partial_json='{"query":"weather"}',
    )
    events = [
        SimpleNamespace(type="content_block_start", index=0, content_block=server_block),
        SimpleNamespace(type="content_block_delta", index=0, delta=input_delta),
        _anthropic_message_stop(),
    ]
    final_block = DumpableBlock({
        "type": "server_tool_use",
        "id": "srv_1",
        "name": "web_search",
        "input": {"query": "weather"},
    })
    provider = _anthropic_provider(FakeAnthropicStream(
        events,
        final_message=_anthropic_message("pause_turn", content=[final_block]),
    ))
    call = _call()

    response = asyncio.run(provider._stream_chat(call=call, model="stream-test"))

    assert response.finish_reason == "pause_turn"
    assert response.tool_calls == {}
    assert call.tool_fragments == {}
    assert call.tool_fragment_state == "none"


@pytest.mark.parametrize(
    "terminal_error",
    [
        LLMStreamResponseError("stream interrupted"),
        EOFError("unexpected EOF"),
    ],
)
def test_anthropic_keeps_partial_tool_json_when_stream_breaks(
    terminal_error: BaseException,
) -> None:
    """Anthropic 工具参数中途断流时调用上下文应保持 partial。

    Args:
        terminal_error: 事件耗尽后抛出的中断异常。

    Returns:
        None。
    """
    tool_block = SimpleNamespace(type="tool_use", id="toolu_1", name="lookup")
    input_delta = SimpleNamespace(type="input_json_delta", partial_json='{"q":')
    events = [
        SimpleNamespace(type="content_block_start", index=0, content_block=tool_block),
        SimpleNamespace(type="content_block_delta", index=0, delta=input_delta),
    ]
    provider = _anthropic_provider(
        FakeAnthropicStream(
            events,
            final_message=None,
            terminal_error=terminal_error,
        )
    )
    call = _call()

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(provider._stream_chat(call=call, model="stream-test"))

    assert call.tool_fragment_state == "partial"
    assert call.tool_fragments == {
        0: {"id": "toolu_1", "name": "lookup", "arguments": '{"q":'}
    }
