"""LLM 重试退避与调用上下文测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import logging
import math
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING

import pytest

from src.events import EventBus
from src.events.types import Event, LLMRetrying, OutputRequested
from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.anthropic import AnthropicProvider
from src.llm.deepseek import DeepSeekProvider
from src.llm.moonshot import MoonshotProvider
from src.llm.ollama import OllamaProvider
from src.llm.openai import OpenAIProvider
from src.llm.errors import LLMCallError, LLMErrorInfo, LLMErrorKind, LLMStreamResponseError
from src.llm.retry import RetryConfig, RetryPolicy

if TYPE_CHECKING:
    from src.tools import ToolDict


def _info(
    *,
    retry_after: str | None = None,
    retry_after_ms: float | None = None,
) -> LLMErrorInfo:
    """构造可重试错误信息。

    Args:
        retry_after: Retry-After 原始值。
        retry_after_ms: retry-after-ms 毫秒值。

    Returns:
        可重试的限流错误信息。
    """
    return LLMErrorInfo(
        kind=LLMErrorKind.RATE_LIMIT,
        message="rate limited",
        retryable=True,
        retry_after=retry_after,
        retry_after_ms=retry_after_ms,
        original_exception_type="ProviderError",
    )


def test_retry_after_ms_has_highest_priority() -> None:
    """retry-after-ms 应优先于 Retry-After 与指数退避。

    Returns:
        None。
    """
    policy = RetryPolicy(
        RetryConfig(max_attempts=3, base_delay_seconds=2, max_delay_seconds=60),
        random_value=lambda: 0.0,
    )

    assert policy.delay(_info(retry_after="9", retry_after_ms=1250), attempt=1) == 1.25


def test_retry_after_seconds_precedes_exponential_backoff() -> None:
    """数字 Retry-After 应按秒使用且跳过抖动。

    Returns:
        None。
    """
    policy = RetryPolicy(random_value=lambda: 0.0)

    assert policy.delay(_info(retry_after="7"), attempt=1) == 7


@pytest.mark.parametrize(
    "retry_metadata",
    [
        {"retry_after_ms": float("nan")},
        {"retry_after_ms": float("inf")},
        {"retry_after_ms": float("-inf")},
        {"retry_after": "nan"},
        {"retry_after": "inf"},
        {"retry_after": "-inf"},
    ],
)
def test_non_finite_retry_after_values_fall_back_to_exponential_delay(
    retry_metadata: dict[str, str | float],
) -> None:
    """非有限 Retry-After 值应被拒绝并使用有限指数退避。

    Args:
        retry_metadata: 包含非法 Retry-After 秒数或毫秒数的错误元数据。

    Returns:
        None。
    """
    policy = RetryPolicy(random_value=lambda: 0.0)

    delay = policy.delay(_info(**retry_metadata), attempt=1)

    assert delay == 1.5
    assert math.isfinite(delay)


def test_retry_after_http_date_uses_injected_current_time() -> None:
    """HTTP date Retry-After 应相对注入时间计算等待秒数。

    Returns:
        None。
    """
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    policy = RetryPolicy(now=lambda: now, random_value=lambda: 0.0)

    delay = policy.delay(
        _info(retry_after="Wed, 22 Jul 2026 12:00:09 GMT"),
        attempt=1,
    )

    assert delay == 9


@pytest.mark.parametrize(
    ("attempt", "random_value", "expected"),
    [
        (1, 0.0, 1.5),
        (1, 1.0, 2.0),
        (2, 0.0, 3.0),
        (2, 1.0, 4.0),
    ],
)
def test_exponential_backoff_uses_bounded_jitter(
    attempt: int,
    random_value: float,
    expected: float,
) -> None:
    """指数退避应使用 0.75 到 1.0 的确定性抖动。

    Args:
        attempt: 已失败的 1 基尝试序号。
        random_value: 注入的零到一随机值。
        expected: 预期等待秒数。

    Returns:
        None。
    """
    policy = RetryPolicy(random_value=lambda: random_value)

    assert policy.delay(_info(), attempt=attempt) == expected


def test_all_delays_are_capped_by_max_delay() -> None:
    """响应头与退避计算结果都不得超过最大等待时间。

    Returns:
        None。
    """
    policy = RetryPolicy(
        RetryConfig(max_attempts=3, base_delay_seconds=2, max_delay_seconds=5),
        random_value=lambda: 1.0,
    )

    assert policy.delay(_info(retry_after_ms=9000), attempt=1) == 5
    assert policy.delay(_info(retry_after="20"), attempt=1) == 5
    assert policy.delay(_info(), attempt=3) == 5


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        1.0,
        "3",
        None,
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
    ids=[
        "true",
        "false",
        "zero",
        "negative",
        "float",
        "string",
        "none",
        "object",
        "nan",
        "positive-infinity",
        "negative-infinity",
    ],
)
def test_retry_config_rejects_invalid_max_attempts(value: object) -> None:
    """最大尝试次数必须是非 bool 且大于等于一的整数。

    Args:
        value: 待验证的最大尝试次数。

    Returns:
        None。
    """
    with pytest.raises(ValueError, match="max_attempts"):
        RetryConfig(max_attempts=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["base_delay_seconds", "max_delay_seconds"],
)
@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        "2",
        None,
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
    ids=[
        "true",
        "false",
        "zero",
        "negative",
        "string",
        "none",
        "object",
        "nan",
        "positive-infinity",
        "negative-infinity",
    ],
)
def test_retry_config_rejects_invalid_delays(field: str, value: object) -> None:
    """基础与最大延迟都必须是非 bool 的有限正数。

    Args:
        field: 待覆盖的延迟配置字段名。
        value: 待验证的延迟值。

    Returns:
        None。
    """
    kwargs: dict[str, object] = {
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 2.0,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        RetryConfig(**kwargs)  # type: ignore[arg-type]


def test_retry_config_rejects_max_delay_below_base_delay() -> None:
    """最大延迟小于基础延迟时拒绝配置。

    Returns:
        None。
    """
    with pytest.raises(ValueError, match="max_delay_seconds"):
        RetryConfig(base_delay_seconds=3, max_delay_seconds=2)


@pytest.mark.parametrize(
    ("max_attempts", "base_delay_seconds", "max_delay_seconds"),
    [
        (1, 1, 1),
        (3, 1.5, 2.5),
        (2, 1, 2.5),
        (4, 1.5, 2),
    ],
)
def test_retry_config_accepts_valid_numeric_values(
    max_attempts: int,
    base_delay_seconds: int | float,
    max_delay_seconds: int | float,
) -> None:
    """合法整数次数及整数或浮点延迟继续被接受。

    Args:
        max_attempts: 合法的最大尝试次数。
        base_delay_seconds: 合法的基础延迟秒数。
        max_delay_seconds: 合法的最大延迟秒数。

    Returns:
        None。
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )

    assert config.max_attempts == max_attempts
    assert config.base_delay_seconds == base_delay_seconds
    assert config.max_delay_seconds == max_delay_seconds


class RetryProvider(LLMProvider):
    """按测试脚本产生响应或异常的 provider。"""

    script: list[object]
    calls: list[LLMCallContext]
    sleeps: list[float]

    def __post_init__(self) -> None:
        """初始化测试状态与基类并发控制。

        Returns:
            None。
        """
        super().__post_init__()
        self.script = []
        self.calls = []
        self.sleeps = []

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """返回测试固定 token 数。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。

        Returns:
            固定值 0。
        """
        return 0

    async def _sleep(self, delay: float) -> None:
        """记录等待时长而不实际等待。

        Args:
            delay: 计划等待秒数。

        Returns:
            None。
        """
        self.sleeps.append(delay)

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        call: LLMCallContext | None = None,
    ) -> LLMResponse:
        """执行脚本中的下一项。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            call: 当前独立调用尝试上下文。

        Returns:
            脚本中的 LLM 响应。

        Raises:
            BaseException: 脚本中的异常项。
        """
        assert call is not None
        self.calls.append(call)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, LLMResponse)
        return item


def _provider(max_attempts: int = 3) -> RetryProvider:
    """构造不连接网络的重试 provider。

    Args:
        max_attempts: 包含首次调用的最大尝试次数。

    Returns:
        初始化完成的测试 provider。
    """
    return RetryProvider(
        api_key="",
        base_url="",
        model="stub",
        event_bus=None,
        max_attempts=max_attempts,
    )


class RecordingEventBus(EventBus):
    """直接记录所有事件的测试事件总线。"""

    def __init__(self) -> None:
        """初始化事件记录列表。

        Returns:
            None。
        """
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        """记录单个事件而不经订阅队列广播。

        Args:
            event: 待记录事件。

        Returns:
            None。
        """
        self.events.append(event)


def test_chat_retries_partial_stream_without_returning_failed_fragments() -> None:
    """部分流失败后应使用新上下文重试且仅返回成功尝试。

    Returns:
        None。
    """
    provider = _provider(max_attempts=2)

    async def first_attempt() -> LLMResponse:
        """该局部协程仅用于类型占位。

        Returns:
            不会实际返回。
        """
        raise AssertionError

    del first_attempt
    provider.script = [LLMStreamResponseError("broken stream"), LLMResponse(content="clean")]
    original_do_chat = provider._do_chat

    async def do_chat_with_partial(*args, **kwargs) -> LLMResponse:
        """首轮先发出部分正文，再执行脚本。

        Args:
            args: 原始位置参数。
            kwargs: 原始关键字参数。

        Returns:
            脚本响应。
        """
        call = kwargs["call"]
        if call.attempt == 1:
            await provider.emit_response_delta("failed-fragment", call=call)
        return await original_do_chat(*args, **kwargs)

    provider._do_chat = do_chat_with_partial  # type: ignore[method-assign]

    response = asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert response.content == "clean"
    assert [call.attempt for call in provider.calls] == [1, 2]
    assert provider.calls[0] is not provider.calls[1]
    assert provider.calls[0].partial_output == "failed-fragment"
    assert provider.calls[1].partial_output == ""
    assert len(provider.sleeps) == 1


def test_chat_terminal_error_reports_attempts_and_last_partial_output() -> None:
    """重试耗尽后应抛结构化终态错误并携带末轮残片。

    Returns:
        None。
    """
    provider = _provider(max_attempts=2)
    provider.script = [LLMStreamResponseError("first"), LLMStreamResponseError("second")]
    original_do_chat = provider._do_chat

    async def do_chat_with_partial(*args, **kwargs) -> LLMResponse:
        """每轮失败前记录各自正文残片。

        Args:
            args: 原始位置参数。
            kwargs: 原始关键字参数。

        Returns:
            脚本响应。
        """
        call = kwargs["call"]
        await provider.emit_response_delta(f"partial-{call.attempt}", call=call)
        return await original_do_chat(*args, **kwargs)

    provider._do_chat = do_chat_with_partial  # type: ignore[method-assign]

    with pytest.raises(LLMCallError) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert raised.value.attempts == 2
    assert raised.value.partial_output == "partial-2"
    assert raised.value.info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert raised.value.__cause__ is not None
    assert provider.calls[0].partial_output == "partial-1"
    assert provider.calls[1].partial_output == "partial-2"


def test_retrying_event_is_the_only_attempt_boundary_and_carries_partial_state() -> None:
    """重试边界应只由携带安全错误及残片状态的 LLMRetrying 表达。

    Returns:
        None。
    """
    event_bus = RecordingEventBus()
    provider = RetryProvider(
        api_key="",
        base_url="",
        model="stub",
        event_bus=event_bus,
        max_attempts=2,
    )
    provider.script = [LLMStreamResponseError("broken stream"), LLMResponse(content="clean")]
    original_do_chat = provider._do_chat

    async def do_chat_with_partial_tool(*args, **kwargs) -> LLMResponse:
        """首轮记录正文和未完成工具参数后执行脚本。

        Args:
            args: 原始位置参数。
            kwargs: 原始关键字参数。

        Returns:
            脚本响应。
        """
        call = kwargs["call"]
        if call.attempt == 1:
            await provider.emit_response_delta("partial", call=call)
            call.record_tool_fragment(0, call_id="call_1", name="lookup", arguments='{"q":')
        return await original_do_chat(*args, **kwargs)

    provider._do_chat = do_chat_with_partial_tool  # type: ignore[method-assign]

    response = asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    retry_events = [event for event in event_bus.events if isinstance(event, LLMRetrying)]
    assert response.content == "clean"
    assert not any(isinstance(event, OutputRequested) for event in event_bus.events)
    assert len(retry_events) == 1
    assert retry_events[0].error_kind == LLMErrorKind.RESPONSE_PROTOCOL.value
    assert retry_events[0].safe_message == "broken stream"
    assert retry_events[0].partial is True
    assert retry_events[0].tool_fragment_state == "partial"
    assert retry_events[0].caller_uuid is None


def test_chat_wraps_unknown_errors_without_retrying() -> None:
    """未知异常不应裸抛或自动重试。

    Returns:
        None。
    """
    provider = _provider(max_attempts=3)
    original = RuntimeError("unexpected")
    provider.script = [original]

    with pytest.raises(LLMCallError) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert raised.value.info.kind is LLMErrorKind.UNKNOWN
    assert raised.value.attempts == 1
    assert raised.value.__cause__ is original
    assert provider.sleeps == []


def test_chat_logging_cannot_replace_error_when_traceback_getter_raises() -> None:
    """未知异常的恶意 traceback getter 不得替换安全终态错误。

    Returns:
        None。
    """

    class MaliciousTracebackError(RuntimeError):
        """读取 traceback 时抛出另一个异常的测试错误。"""

        @property
        def __traceback__(self) -> object:
            """拒绝读取异常堆栈。

            Returns:
                本属性不会返回。

            Raises:
                RuntimeError: 每次读取均抛出固定辅助错误。
            """
            raise RuntimeError("traceback getter exploded")

    provider = _provider(max_attempts=1)
    original = MaliciousTracebackError("opaque provider failure")
    provider.script = [original]

    caught: BaseException | None = None
    try:
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))
    except BaseException as exc:
        caught = exc

    assert isinstance(caught, LLMCallError)
    assert caught.info.kind is LLMErrorKind.UNKNOWN
    assert caught.__cause__ is original


def test_chat_propagates_cancellation_unchanged() -> None:
    """chat 应原样传播 asyncio 取消异常。

    Returns:
        None。
    """
    provider = _provider(max_attempts=3)
    cancelled = asyncio.CancelledError()
    provider.script = [cancelled]

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert raised.value is cancelled
    assert len(provider.calls) == 1


class ConcurrentProvider(RetryProvider):
    """让两个 chat 调用同时进入 provider 的测试实现。"""

    entered: asyncio.Event
    release: asyncio.Event

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        call: LLMCallContext | None = None,
    ) -> LLMResponse:
        """等待另一个调用进入后返回各自身份。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            call: 当前独立调用尝试上下文。

        Returns:
            content 为当前 caller UUID 的响应。
        """
        assert call is not None
        self.calls.append(call)
        if len(self.calls) == 2:
            self.entered.set()
        await self.entered.wait()
        await self.release.wait()
        await self.emit_response_delta(call.caller_uuid or "")
        return LLMResponse(content=call.caller_uuid or "")


def test_concurrent_chat_calls_use_isolated_contexts() -> None:
    """共享 provider 的并发 chat 调用不得共享流状态和身份。

    Returns:
        None。
    """

    async def run() -> tuple[list[LLMResponse], ConcurrentProvider]:
        """并发执行两个 chat 调用。

        Returns:
            两个响应与测试 provider。
        """
        provider = ConcurrentProvider(
            api_key="",
            base_url="",
            model="stub",
            event_bus=None,
            max_attempts=1,
        )
        provider.entered = asyncio.Event()
        provider.release = asyncio.Event()
        tasks = [
            asyncio.create_task(
                provider.chat(
                    [{"role": "user", "content": caller_uuid}],
                    caller_agent_type="worker",
                    caller_uuid=caller_uuid,
                )
            )
            for caller_uuid in ("uuid-a", "uuid-b")
        ]
        await provider.entered.wait()
        provider.release.set()
        return await asyncio.gather(*tasks), provider

    responses, provider = asyncio.run(run())

    assert {response.content for response in responses} == {"uuid-a", "uuid-b"}
    assert len({id(call) for call in provider.calls}) == 2
    assert {call.caller_uuid for call in provider.calls} == {"uuid-a", "uuid-b"}
    assert all(call.attempt == 1 for call in provider.calls)
    assert {call.partial_output for call in provider.calls} == {"uuid-a", "uuid-b"}


def test_deepseek_parser_records_tool_fragment_before_stream_failure() -> None:
    """DeepSeek 工具流断裂时尝试上下文应保留到达的工具片段。

    Returns:
        None。
    """

    class BrokenToolStream:
        """产生一个工具片段后中断的异步流。"""

        def __aiter__(self):
            """返回异步迭代器自身。

            Returns:
                当前流对象。
            """
            return self

        async def __anext__(self):
            """首次返回工具片段，第二次抛出流协议异常。

            Returns:
                模拟 Chat Completions 数据块。

            Raises:
                LLMStreamResponseError: 工具参数尚未完成时模拟连接中断。
            """
            if hasattr(self, "_sent"):
                raise LLMStreamResponseError("stream interrupted")
            self._sent = True
            function = SimpleNamespace(name="lookup", arguments='{"q":')
            tool_chunk = SimpleNamespace(index=0, id="call_1", function=function)
            delta = SimpleNamespace(reasoning_content=None, content=None, tool_calls=[tool_chunk])
            choice = SimpleNamespace(delta=delta, finish_reason=None)
            return SimpleNamespace(usage=None, choices=[choice])

    provider = DeepSeekProvider(
        api_key="test",
        base_url="https://example.test/v1",
        model="deepseek-test",
        event_bus=None,
        max_attempts=1,
    )
    call = LLMCallContext(attempt=1, caller_agent_type="worker", caller_uuid="uuid-tool")

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(provider._parse_stream(BrokenToolStream(), call=call))

    assert call.tool_fragment_state == "partial"
    assert call.tool_fragments == {
        0: {"id": "call_1", "name": "lookup", "arguments": '{"q":'}
    }


def test_anthropic_parser_records_tool_fragment_before_stream_failure() -> None:
    """Anthropic 工具输入流断裂时应保留已到达的 ID、名称和参数。

    Returns:
        None。
    """

    class BrokenAnthropicStream:
        """产生工具开始和参数片段后中断的 Anthropic 异步流。"""

        def __init__(self) -> None:
            """初始化待发送事件。

            Returns:
                None。
            """
            tool_block = SimpleNamespace(type="tool_use", id="toolu_1", name="lookup")
            input_delta = SimpleNamespace(type="input_json_delta", partial_json='{"q":')
            self._events = [
                SimpleNamespace(type="content_block_start", index=0, content_block=tool_block),
                SimpleNamespace(type="content_block_delta", index=0, delta=input_delta),
            ]

        async def __aenter__(self) -> BrokenAnthropicStream:
            """进入异步流上下文。

            Returns:
                当前流对象。
            """
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
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

        def __aiter__(self) -> BrokenAnthropicStream:
            """返回异步迭代器自身。

            Returns:
                当前流对象。
            """
            return self

        async def __anext__(self) -> SimpleNamespace:
            """依次返回事件并在参数未完成时模拟断流。

            Returns:
                下一个 Anthropic 流事件。

            Raises:
                LLMStreamResponseError: 两个工具事件发送完毕后模拟断流。
            """
            if self._events:
                return self._events.pop(0)
            raise LLMStreamResponseError("anthropic stream interrupted")

    class FakeMessages:
        """只提供 messages.stream 的测试客户端接口。"""

        def stream(self, **kwargs: object) -> BrokenAnthropicStream:
            """返回模拟的 Anthropic 流上下文。

            Args:
                kwargs: provider 下发的请求参数，本测试不使用。

            Returns:
                会在工具参数中途断开的流。
            """
            return BrokenAnthropicStream()

    provider = object.__new__(AnthropicProvider)
    provider._client = SimpleNamespace(messages=FakeMessages())
    provider.event_bus = None
    provider.model = "anthropic-test"
    call = LLMCallContext(attempt=1, caller_agent_type="worker", caller_uuid="uuid-anthropic")

    with pytest.raises(LLMStreamResponseError):
        asyncio.run(provider._stream_chat(call=call, model="anthropic-test"))

    assert call.tool_fragment_state == "partial"
    assert call.tool_fragments == {
        0: {"id": "toolu_1", "name": "lookup", "arguments": '{"q":'}
    }


@pytest.mark.parametrize(
    "production_method",
    [
        AnthropicProvider._do_chat,
        DeepSeekProvider._do_chat,
        MoonshotProvider._do_chat,
        OllamaProvider._do_chat,
        OpenAIProvider._do_chat,
        AnthropicProvider._stream_chat,
        DeepSeekProvider._parse_stream,
        MoonshotProvider._parse_stream,
        OllamaProvider._parse_stream,
        OpenAIProvider._parse_stream,
    ],
)
def test_production_provider_call_context_is_required(production_method: object) -> None:
    """生产 provider 调用与流解析方法必须显式要求尝试上下文。

    Args:
        production_method: 待检查的 provider 方法。

    Returns:
        None。
    """
    call_parameter = inspect.signature(production_method).parameters["call"]

    assert call_parameter.default is inspect.Parameter.empty


def test_sanitized_external_metadata_cannot_inject_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """结构化失败日志不得包含外部元数据中的换行或凭据。

    Args:
        caplog: pytest 日志捕获 fixture。

    Returns:
        None。
    """

    class InjectedMetadataError(Exception):
        """携带恶意供应商元数据的测试异常。"""

        status_code = 429
        body = {
            "error": {
                "code": "rate_limit_exceeded\nBearer code-log-secret",
                "message": "rate limited\napi_key=message-log-secret",
            }
        }
        request_id = "req\nBearer request-log-secret"
        response = SimpleNamespace(
            status_code=429,
            headers={"retry-after": "5\nBearer retry-log-secret"},
        )

    provider = _provider(max_attempts=1)
    provider.script = [InjectedMetadataError("failed")]
    caplog.set_level(logging.WARNING, logger="src.llm.base")

    with pytest.raises(LLMCallError):
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert "\nBearer" not in caplog.text
    assert "code-log-secret" not in caplog.text
    assert "message-log-secret" not in caplog.text
    assert "request-log-secret" not in caplog.text
    assert "retry-log-secret" not in caplog.text
