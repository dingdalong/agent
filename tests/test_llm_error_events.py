"""LLM 错误事件、路由、读模型与终端展示的回归测试。"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from io import StringIO
from pathlib import Path
import time
from typing import Any, get_args
import uuid

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from rich.text import Text

import src.events as events_package
from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext
from src.events import AgentEvent
from src.events.types import (
    Event,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.base import UserInterface
from src.interfaces.inline.controller import InlineController
from src.interfaces.inline.plain import PlainFrontend
from src.interfaces.output_router import OutputRouter
from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.errors import LLMCallError, LLMErrorKind, LLMStreamResponseError
from src.mgr.compact_mgr import CompactMgr, _SummaryRequest


class RecordingEventBus:
    """按发射顺序保存事件的轻量总线。"""

    def __init__(self) -> None:
        """初始化空事件列表。

        Returns:
            None。
        """
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        """保存一个事件。

        Args:
            event: 待保存事件。

        Returns:
            None。
        """
        self.events.append(event)


class FaultingEventBus(RecordingEventBus):
    """在指定遥测事件类型上抛出固定异常的测试总线。"""

    def __init__(
        self,
        fault_type: type[Event],
        fault: BaseException | None = None,
    ) -> None:
        """初始化故障类型与异常。

        Args:
            fault_type: 触发异常的事件类型。
            fault: 发布时抛出的异常；缺省为带敏感文本的 RuntimeError。

        Returns:
            None。
        """
        super().__init__()
        self.fault_type = fault_type
        self.fault = fault or RuntimeError("telemetry-secret")

    async def emit(self, event: Event) -> None:
        """记录事件并在命中故障类型时抛出异常。

        Args:
            event: 待发布事件。

        Returns:
            None。

        Raises:
            BaseException: 命中故障类型时抛出配置的异常。
        """
        await super().emit(event)
        if isinstance(event, self.fault_type):
            raise self.fault


class ScriptedProvider(LLMProvider):
    """逐次返回或抛出脚本结果的测试 provider。"""

    def __init__(
        self,
        event_bus: RecordingEventBus,
        outcomes: list[LLMResponse | BaseException],
        *,
        max_attempts: int = 2,
        partial_attempts: set[int] | None = None,
    ) -> None:
        """初始化脚本、尝试上限与残片尝试集合。

        Args:
            event_bus: 记录 provider 事件的总线。
            outcomes: 每次尝试依次消费的响应或异常。
            max_attempts: 最大尝试次数。
            partial_attempts: 抛错前先发正文及工具残片的尝试序号。

        Returns:
            None。
        """
        super().__init__(
            api_key="",
            base_url="",
            model="stub-model",
            event_bus=event_bus,  # type: ignore[arg-type]
            max_attempts=max_attempts,
            base_delay_seconds=0.01,
            max_delay_seconds=0.01,
            context_limit=1000,
        )
        self.outcomes = list(outcomes)
        self.partial_attempts = partial_attempts or set()
        self.sleeps: list[float] = []
        self.attempts_seen: list[int] = []

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[dict] | None = None,
    ) -> int:
        """返回固定输入 token 估算。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。

        Returns:
            固定值 12。
        """
        del messages, prompt, tools
        return 12

    async def _sleep(self, delay: float) -> None:
        """记录退避秒数且不实际等待。

        Args:
            delay: 本次退避秒数。

        Returns:
            None。
        """
        self.sleeps.append(delay)

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[dict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """消费当前脚本结果，必要时先产生失败残片。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位。
            call: 当前尝试上下文。

        Returns:
            当前脚本响应。

        Raises:
            BaseException: 当前脚本项为异常时原样抛出。
        """
        del messages, prompt, tools, temperature, tool_choice, enable_thinking
        del reasoning_effort_override
        self.attempts_seen.append(call.attempt)
        if call.attempt in self.partial_attempts:
            await self.emit_response_delta(f"partial-{call.attempt}", call=call)
            call.record_tool_fragment(
                0,
                call_id=f"call-{call.attempt}",
                name="lookup",
                arguments='{"q":',
            )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def clear_reasoning_content(self, message: object) -> None:
        """满足 provider 接口且不修改消息。

        Args:
            message: 待处理消息。

        Returns:
            None。
        """
        del message


class RecordingUI:
    """只保存路由转发事件的轻量 UI。"""

    def __init__(self) -> None:
        """初始化空事件列表。

        Returns:
            None。
        """
        self.events: list[Event] = []

    async def on_event(self, event: Event) -> None:
        """保存一个可见事件。

        Args:
            event: 路由判定可见的事件。

        Returns:
            None。
        """
        self.events.append(event)


class StreamOrderUI(UserInterface):
    """记录 Markdown 收尾与错误 hook 调用顺序的 UI。"""

    def __init__(self) -> None:
        """初始化顺序记录。

        Returns:
            None。
        """
        super().__init__()
        self.order: list[str] = []

    async def _write(self, message: str, markdown: bool = False) -> None:
        """忽略文本写入。

        Args:
            message: 输出文本。
            markdown: 是否按 Markdown 渲染。

        Returns:
            None。
        """
        del message, markdown

    async def _read_input(self, prompt: str, default: str = "", markdown: bool = False) -> str:
        """返回固定输入。

        Args:
            prompt: 输入提示。
            default: 默认文本。
            markdown: 是否按 Markdown 渲染提示。

        Returns:
            固定文本。
        """
        del prompt, default, markdown
        return "input"

    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
        suggested_rules: list[str] | None = None,
        mcp_server_rule: str | None = None,
    ) -> str:
        """返回固定权限选择。

        Args:
            tool_name: 工具名。
            detail: 工具详情。
            suggested_rules: 建议规则。
            mcp_server_rule: MCP server 规则。

        Returns:
            固定允许选择。
        """
        del tool_name, detail, suggested_rules, mcp_server_rule
        return "yes"

    async def _read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """返回默认选项。

        Args:
            prompt: 菜单提示。
            options: 菜单选项。
            default_index: 默认下标。
            markdown: 是否渲染 Markdown。

        Returns:
            默认选项值或空串。
        """
        del prompt, markdown
        return options[default_index][0] if options else ""

    async def _read_form(self, prompt: str, questions: list[Any], markdown: bool = False) -> str:
        """返回空表单 JSON。

        Args:
            prompt: 表单提示。
            questions: 表单问题。
            markdown: 是否渲染 Markdown。

        Returns:
            空表单结果。
        """
        del prompt, questions, markdown
        return '{"answers": [], "discussion": ""}'

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """返回空组合输入。

        Args:
            prompt: 菜单提示。
            options: 菜单选项。
            descriptions: 选项说明。
            input_placeholder: 输入占位。
            default_index: 默认下标。
            markdown: 是否渲染 Markdown。

        Returns:
            空串。
        """
        del prompt, options, descriptions, input_placeholder, default_index, markdown
        return ""

    async def _read_transcript_view(self, uuid: str) -> str:
        """关闭只读转录。

        Args:
            uuid: 目标 agent UUID。

        Returns:
            空串。
        """
        del uuid
        return ""

    async def _end_thinking_if_needed(self) -> None:
        """记录思考流收尾。

        Returns:
            None。
        """
        self.order.append("end-thinking")

    async def _end_response_if_needed(self) -> None:
        """记录回应流收尾。

        Returns:
            None。
        """
        self.order.append("end-response")

    async def on_llm_retrying(self, event: LLMRetrying) -> None:
        """记录重试 hook。

        Args:
            event: 重试事件。

        Returns:
            None。
        """
        del event
        self.order.append("retry")

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        """记录终态失败 hook。

        Args:
            event: 终态失败事件。

        Returns:
            None。
        """
        del event
        self.order.append("failed")


def _started(uuid_value: str, *, attempt: int = 1, max_attempts: int = 3) -> LLMCallStarted:
    """构造带身份和尝试序号的调用开始事件。

    Args:
        uuid_value: caller UUID。
        attempt: 当前尝试序号。
        max_attempts: 最大尝试次数。

    Returns:
        调用开始事件。
    """
    return LLMCallStarted(
        timestamp=1.0,
        source="stub",
        caller_agent_type="worker",
        caller_uuid=uuid_value,
        attempt=attempt,
        max_attempts=max_attempts,
    )


def _retry(uuid_value: str, *, partial: bool = True) -> LLMRetrying:
    """构造带安全错误字段的重试事件。

    Args:
        uuid_value: caller UUID。
        partial: 是否已产生残片。

    Returns:
        重试事件。
    """
    return LLMRetrying(
        timestamp=2.0,
        source="stub",
        caller_agent_type="worker",
        caller_uuid=uuid_value,
        error_kind="service",
        safe_message="服务暂时不可用",
        partial=partial,
        tool_fragment_state="partial" if partial else "none",
        attempt=1,
        max_attempts=3,
        wait_seconds=1.2,
    )


def _failed(uuid_value: str) -> LLMCallFailed:
    """构造带安全诊断字段的终态失败事件。

    Args:
        uuid_value: caller UUID。

    Returns:
        终态失败事件。
    """
    return LLMCallFailed(
        timestamp=3.0,
        source="stub",
        caller_agent_type="worker",
        caller_uuid=uuid_value,
        error_kind="service",
        safe_message="服务仍不可用",
        attempts=3,
        partial=True,
        tool_fragment_state="partial",
        status_code=503,
        provider_code="server_error",
        request_id="req-safe",
        diagnostic_id="diag-safe",
    )


def test_events_package_exports_terminal_failure_without_legacy_error_type() -> None:
    """包出口与 AgentEvent 包含错误事件，重试事件彻底移除 error_type。"""
    retry_fields = {item.name for item in fields(LLMRetrying)}

    assert "error_type" not in retry_fields
    assert events_package.LLMCallFailed is LLMCallFailed
    assert LLMCallFailed in get_args(AgentEvent)
    assert "LLMCallFailed" in events_package.__all__
    assert "LLMRetrying" in events_package.__all__
    assert "LLMCallStarted" in events_package.__all__


def test_provider_emits_attempt_boundaries_retry_and_one_terminal_failure() -> None:
    """每次尝试先发 start，重试只发 retry，耗尽只发一次安全 failure。"""
    bus = RecordingEventBus()
    error_one = LLMStreamResponseError(
        "服务暂时不可用",
        code="server_error",
        status_code=503,
        request_id="req-one",
    )
    error_two = LLMStreamResponseError(
        "服务仍不可用",
        code="server_error",
        status_code=503,
        request_id="req-two",
    )
    provider = ScriptedProvider(
        bus,
        [error_one, error_two],
        max_attempts=2,
        partial_attempts={1, 2},
    )

    with pytest.raises(LLMCallError) as raised:
        asyncio.run(provider.chat(
            [{"role": "user", "content": "hello"}],
            caller_agent_type="main",
            caller_uuid="foreground",
        ))

    boundaries = [
        event
        for event in bus.events
        if isinstance(event, (LLMCallStarted, LLMRetrying, LLMCallFailed))
    ]
    assert [type(event) for event in boundaries] == [
        LLMCallStarted,
        LLMRetrying,
        LLMCallStarted,
        LLMCallFailed,
    ]
    starts = [event for event in boundaries if isinstance(event, LLMCallStarted)]
    assert [(event.attempt, event.max_attempts) for event in starts] == [(1, 2), (2, 2)]

    retry = next(event for event in boundaries if isinstance(event, LLMRetrying))
    assert retry.error_kind == "service"
    assert retry.safe_message == "服务暂时不可用"
    assert retry.partial is True
    assert retry.tool_fragment_state == "partial"
    assert retry.attempt == 1
    assert retry.max_attempts == 2

    failures = [event for event in boundaries if isinstance(event, LLMCallFailed)]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.error_kind == "service"
    assert failure.safe_message == "服务仍不可用"
    assert failure.attempts == 2
    assert failure.partial is True
    assert failure.tool_fragment_state == "partial"
    assert failure.status_code == 503
    assert failure.provider_code == "server_error"
    assert failure.request_id == "req-two"
    assert failure.diagnostic_id == raised.value.diagnostic_id
    assert failure.caller_uuid == "foreground"


def test_provider_marks_length_response_partial_after_thinking_only_delta() -> None:
    """仅收到思考增量时，成功返回的 length 响应仍保留稳定残片标记。"""
    bus = RecordingEventBus()
    provider = ScriptedProvider(
        bus,
        [LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": ""},
        )],
        max_attempts=1,
    )
    original_do_chat = provider._do_chat

    async def do_chat_with_thinking(*args: Any, **kwargs: Any) -> LLMResponse:
        """发出思考增量后返回脚本响应。

        Args:
            args: 原 provider 调用位置参数。
            kwargs: 原 provider 调用关键字参数，包含 call。

        Returns:
            脚本中的 length 响应。
        """
        await provider.emit_thinking_delta("thinking fragment", call=kwargs["call"])
        return await original_do_chat(*args, **kwargs)

    provider._do_chat = do_chat_with_thinking  # type: ignore[method-assign]

    response = asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert response.has_partial_data is True
    # 仅有思考增量、无正文与工具调用的 length 响应归为思考阶段截断。
    assert response.truncation_kind == "thinking"


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError("cancel"), KeyboardInterrupt("interrupt"), SystemExit("exit")],
)
def test_provider_control_flow_never_emits_terminal_failure(control_error: BaseException) -> None:
    """取消、中断和退出保持控制流语义，不产生 retry 或 failure。"""
    bus = RecordingEventBus()
    provider = ScriptedProvider(bus, [control_error], max_attempts=2)

    with pytest.raises(type(control_error)):
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert len([event for event in bus.events if isinstance(event, LLMCallStarted)]) == 1
    assert not any(isinstance(event, (LLMRetrying, LLMCallFailed)) for event in bus.events)


def test_safe_telemetry_boundary_logs_only_event_and_exception_types(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """普通发布异常只留下安全类型信息，不传播异常文本。

    Args:
        caplog: pytest 日志捕获器。
    """
    bus = FaultingEventBus(ResponseDelta)
    event = ResponseDelta(timestamp=1.0, source="stub", content="body")

    asyncio.run(events_package.emit_telemetry_safely(bus, event))

    assert event.type in caplog.text
    assert "RuntimeError" in caplog.text
    assert "telemetry-secret" not in caplog.text


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError("cancel"), KeyboardInterrupt("interrupt"), SystemExit("exit")],
)
def test_safe_telemetry_boundary_propagates_control_flow(
    control_error: BaseException,
) -> None:
    """遥测发布的取消、中断与退出异常原样传播。

    Args:
        control_error: 总线抛出的控制流异常。
    """
    bus = FaultingEventBus(ResponseDelta, control_error)
    event = ResponseDelta(timestamp=1.0, source="stub", content="body")

    with pytest.raises(type(control_error)) as raised:
        asyncio.run(events_package.emit_telemetry_safely(bus, event))

    assert raised.value is control_error


@pytest.mark.parametrize(
    "fault_type",
    [LLMCallStarted, ResponseDelta, ThinkingDelta, LLMCallCompleted],
)
def test_successful_provider_call_survives_each_telemetry_publish_failure(
    fault_type: type[Event],
) -> None:
    """开始、正文、思考或完成事件故障均不重放成功请求。

    Args:
        fault_type: 本次模拟发布失败的遥测事件类型。
    """
    bus = FaultingEventBus(fault_type)
    provider = ScriptedProvider(
        bus,
        [LLMResponse(content="ok", finish_reason="stop")],
        max_attempts=2,
    )
    original_do_chat = provider._do_chat

    async def do_chat_with_deltas(*args: Any, **kwargs: Any) -> LLMResponse:
        """发出正文和思考遥测后返回脚本响应。

        Args:
            args: 原 provider 调用位置参数。
            kwargs: 原 provider 调用关键字参数，包含 call。

        Returns:
            脚本成功响应。
        """
        call = kwargs["call"]
        await provider.emit_response_delta("visible", call=call)
        await provider.emit_thinking_delta("thinking", call=call)
        return await original_do_chat(*args, **kwargs)

    provider._do_chat = do_chat_with_deltas  # type: ignore[method-assign]

    response = asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert response.content == "ok"
    assert provider.attempts_seen == [1]
    assert provider.sleeps == []


def test_retry_event_publish_failure_does_not_interrupt_backoff() -> None:
    """Retrying 发布失败不打断退避和下一次 provider 尝试。"""
    bus = FaultingEventBus(LLMRetrying)
    provider = ScriptedProvider(
        bus,
        [LLMStreamResponseError("broken"), LLMResponse(content="ok", finish_reason="stop")],
        max_attempts=2,
    )

    response = asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert response.content == "ok"
    assert provider.attempts_seen == [1, 2]
    assert len(provider.sleeps) == 1


def test_failed_event_publish_failure_preserves_terminal_llm_error() -> None:
    """Failed 发布失败不覆盖原始结构化 LLMCallError。"""
    bus = FaultingEventBus(LLMCallFailed)
    provider = ScriptedProvider(
        bus,
        [LLMStreamResponseError("blocked", code="content_filter")],
        max_attempts=2,
    )

    with pytest.raises(LLMCallError) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))

    assert raised.value.info.kind is LLMErrorKind.CONTENT_POLICY
    assert raised.value.attempts == 1
    assert provider.attempts_seen == [1]
    assert provider.sleeps == []


@pytest.mark.parametrize("passthrough", [False, True])
def test_router_keeps_background_llm_boundaries_silent_but_recorded(
    passthrough: bool,
) -> None:
    """TTY 与 passthrough 非 TTY 均不展示后台 LLM 边界，但 Store 完整记录。"""
    store = AgentViewStore()
    store.register_foreground("foreground", "main")
    ui = RecordingUI()
    router = OutputRouter(ui, store, passthrough=passthrough)  # type: ignore[arg-type]

    for event in (_started("background"), _retry("background"), _failed("background")):
        asyncio.run(router.dispatch(event))

    assert ui.events == []
    assert [kind for kind, _text in store.transcript_segments("background")] == [
        "retry",
        "error",
    ]
    assert store.agent_snapshot("background").activity == "失败"  # type: ignore[union-attr]


def test_router_forwards_foreground_boundaries_and_preserves_other_passthrough() -> None:
    """前台 LLM 边界可见，passthrough 下其它后台正文保持原有转发行为。"""
    store = AgentViewStore()
    store.register_foreground("foreground", "main")
    ui = RecordingUI()
    router = OutputRouter(ui, store, passthrough=True)  # type: ignore[arg-type]
    foreground_events = (_started("foreground"), _retry("foreground"), _failed("foreground"))
    background_delta = ResponseDelta(
        timestamp=4.0,
        source="stub",
        caller_agent_type="worker",
        caller_uuid="background",
        content="background body",
    )

    for event in (*foreground_events, background_delta):
        asyncio.run(router.dispatch(event))

    assert ui.events == [*foreground_events, background_delta]


def _length_retry(uuid_value: str) -> LLMLengthRetrying:
    """构造带截断分类与恢复策略的长度自动恢复事件。

    Args:
        uuid_value: caller UUID。

    Returns:
        长度自动恢复进度事件。
    """
    return LLMLengthRetrying(
        timestamp=2.5,
        source="stub",
        caller_agent_type="worker",
        caller_uuid=uuid_value,
        truncation_kind="thinking",
        strategy="regenerate-lower-effort",
        effort="high",
        attempt=1,
        max_attempts=3,
    )


def test_router_forwards_foreground_length_retry_and_records_it() -> None:
    """前台长度自动恢复事件可见并在 Store 留下恢复转录段。"""
    store = AgentViewStore()
    store.register_foreground("foreground", "main")
    ui = RecordingUI()
    router = OutputRouter(ui, store)  # type: ignore[arg-type]
    event = _length_retry("foreground")

    asyncio.run(router.dispatch(event))

    assert ui.events == [event]
    assert [kind for kind, _text in store.transcript_segments("foreground")] == ["retry"]
    assert store.agent_snapshot("foreground").activity == "恢复中"  # type: ignore[union-attr]


def test_router_keeps_background_length_retry_silent_but_recorded() -> None:
    """后台长度自动恢复事件不展示，但 Store 完整记录恢复段。"""
    store = AgentViewStore()
    store.register_foreground("foreground", "main")
    ui = RecordingUI()
    router = OutputRouter(ui, store)  # type: ignore[arg-type]
    event = _length_retry("background")

    asyncio.run(router.dispatch(event))

    assert ui.events == []
    assert [kind for kind, _text in store.transcript_segments("background")] == ["retry"]


def test_router_does_not_treat_unidentified_boundary_as_foreground() -> None:
    """尚未注册前台 UUID 时，不把无身份 LLM 边界误判为前台事件。"""
    store = AgentViewStore()
    ui = RecordingUI()
    router = OutputRouter(ui, store, passthrough=True)  # type: ignore[arg-type]
    event = LLMCallStarted(timestamp=1.0, source="stub", caller_uuid=None)

    asyncio.run(router.dispatch(event))

    assert ui.events == []


def test_compact_summary_events_keep_agent_identity_and_reach_foreground(
    tmp_path: Path,
) -> None:
    """CompactMgr 的内部摘要调用沿用所属 agent 身份并可被前台路由。"""
    bus = RecordingEventBus()
    provider = ScriptedProvider(
        bus,
        [LLMResponse(
            content="摘要",
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "摘要"},
        )],
        max_attempts=1,
    )
    manager = CompactMgr(
        llm=provider,
        workdir=tmp_path,
        caller_agent_type="main",
        caller_uuid="foreground",
    )

    summary = asyncio.run(manager._call_summary_request(
        _SummaryRequest(prompt="请摘要", estimated_tokens=2),
    ))

    assert summary == "摘要"
    started = next(event for event in bus.events if isinstance(event, LLMCallStarted))
    assert started.caller_agent_type == "main"
    assert started.caller_uuid == "foreground"

    store = AgentViewStore()
    store.register_foreground("foreground", "main")
    ui = RecordingUI()
    router = OutputRouter(ui, store)
    asyncio.run(router.dispatch(started))
    assert ui.events == [started]


def test_store_segments_retry_and_failure_without_merging_next_response() -> None:
    """失败残片、retry、新尝试正文和终态 error 是四个独立转录分段。"""
    store = AgentViewStore(transcript_limit=8)
    store.register_foreground("foreground", "main")
    store.record(ResponseDelta(
        timestamp=1.0,
        source="stub",
        caller_agent_type="main",
        caller_uuid="foreground",
        content="broken fragment",
    ))
    store.record(_retry("foreground"))
    store.record(_started("foreground", attempt=2, max_attempts=3))

    assert store.agent_snapshot("foreground").activity == "等待响应 2/3"  # type: ignore[union-attr]

    store.record(ResponseDelta(
        timestamp=3.0,
        source="stub",
        caller_agent_type="main",
        caller_uuid="foreground",
        content="clean response",
    ))
    store.record(_failed("foreground"))

    segments = store.transcript_segments("foreground")
    assert [kind for kind, _text in segments] == ["response", "retry", "response", "error"]
    assert segments[0][1] == "broken fragment"
    assert "1/3" in segments[1][1]
    assert "service" in segments[1][1]
    assert "服务暂时不可用" in segments[1][1]
    assert "partial=true" in segments[1][1]
    assert "tool=partial" in segments[1][1]
    assert segments[2][1] == "clean response"
    assert "服务仍不可用" in segments[3][1]
    assert store.agent_snapshot("foreground").activity == "失败"  # type: ignore[union-attr]


def test_store_error_segments_obey_transcript_limit() -> None:
    """retry/error 与其它转录一样受 transcript_limit 约束。"""
    store = AgentViewStore(transcript_limit=2)
    store.register_foreground("foreground", "main")
    store.record(ResponseDelta(
        timestamp=1.0,
        source="stub",
        caller_uuid="foreground",
        content="old fragment",
    ))
    store.record(_retry("foreground"))
    store.record(_failed("foreground"))

    assert [kind for kind, _text in store.transcript_segments("foreground")] == [
        "retry",
        "error",
    ]


@pytest.mark.parametrize(
    ("event", "hook"),
    [(_retry("foreground"), "retry"), (_failed("foreground"), "failed")],
)
def test_error_events_end_markdown_streams_before_ui_hook(event: Event, hook: str) -> None:
    """Retrying/Failed 均在 hook 前依次收尾思考与回应 Markdown 流。"""
    ui = StreamOrderUI()

    asyncio.run(ui.on_event(event))

    assert ui.order == ["end-thinking", "end-response", hook]


def test_tty_partial_retry_prints_separator_and_countdown_uses_safe_category() -> None:
    """TTY 有残片时永久打印尝试分隔，倒计时使用 kind 与安全摘要。"""
    ui = InlineController(AgentViewStore())
    ui._tty = True
    printed: list[str] = []

    def record_print(content: str | Text, *, style: str = "", end: str = "\n") -> None:
        """保存 Rich 输出的纯文本。

        Args:
            content: 输出内容。
            style: Rich 样式。
            end: 输出结尾。

        Returns:
            None。
        """
        del style
        printed.append((content.plain if isinstance(content, Text) else content) + end)

    ui._print_rich = record_print  # type: ignore[method-assign]

    asyncio.run(ui.on_llm_retrying(_retry("foreground", partial=True)))

    assert any("尝试 1/3 失败，将重试" in line for line in printed)
    assert any("service" in line and "服务暂时不可用" in line for line in printed)
    countdown = Text()
    ui._append_retry_countdown(countdown, time.monotonic())
    assert "service" in countdown.plain
    assert "服务暂时不可用" in countdown.plain


def test_plain_retry_and_terminal_failure_are_permanent_safe_lines() -> None:
    """非 TTY 每次重试与终态失败各打印一行，并展示安全关联 ID。"""
    output = StringIO()
    ui = InlineController(AgentViewStore())
    ui._tty = False
    ui._plain = PlainFrontend(output)

    asyncio.run(ui.on_llm_retrying(_retry("foreground", partial=False)))
    ui._retry_deadline = time.monotonic() + 10
    asyncio.run(ui.on_llm_call_failed(_failed("foreground")))

    rendered = output.getvalue()
    assert "service" in rendered
    assert "服务暂时不可用" in rendered
    assert "1/3" in rendered
    assert "服务仍不可用" in rendered
    assert "request_id=req-safe" in rendered
    assert "diagnostic_id=diag-safe" in rendered
    assert "RuntimeError" not in rendered
    assert ui._retry_deadline is None
    assert ui._activity == "失败"


def test_completed_agent_panel_keeps_error_diagnostics_without_stream_body_duplication() -> None:
    """完成态原始消息视图追加 retry/error 诊断，但不重复实时正文。"""
    store = AgentViewStore()
    subagent_uuid = "background"
    store.record(ResponseDelta(
        timestamp=1.0,
        source="stub",
        caller_agent_type="worker",
        caller_uuid=subagent_uuid,
        content="stream-only duplicate",
    ))
    store.record(_retry(subagent_uuid))
    store.record(_failed(subagent_uuid))
    store.record(SubagentLifecycle(
        timestamp=4.0,
        source="subagent",
        agent_uuid=subagent_uuid,
        agent_type="worker",
        phase="end",
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "raw final body"},
        ],
    ))
    store.flush_completed()
    ui = InlineController(store)
    ui._render_width = 120
    ui._viewing_uuid = subagent_uuid

    rendered = fragment_list_to_text(to_formatted_text(ui._render_transcript_panel()))

    assert "request_id=req-safe" in rendered
    assert "diagnostic_id=diag-safe" in rendered
    assert "raw final body" in rendered
    assert "stream-only duplicate" not in rendered


def test_turn_reset_and_reload_clear_all_retry_status_fields() -> None:
    """回合重置与 /clear 均清空倒计时和全部 retry 文案、序号字段。"""
    ui = InlineController(AgentViewStore())

    for clear in (ui._reset_turn_status, ui.reload):
        ui._retry_deadline = time.monotonic() + 10
        ui._retry_error_kind = "service"
        ui._retry_safe_message = "stale"
        ui._retry_attempt = 2
        ui._retry_max = 3

        clear()

        assert ui._retry_deadline is None
        assert ui._retry_error_kind == ""
        assert ui._retry_safe_message == ""
        assert ui._retry_attempt == 0
        assert ui._retry_max == 0


class IdentityNormalizer:
    """仅复制消息列表的长度恢复测试 normalizer。"""

    def normalize_messages(self, messages: list[dict]) -> list[dict]:
        """复制消息列表。

        Args:
            messages: 待复制消息。

        Returns:
            新列表。
        """
        return list(messages)


def test_length_recovery_exhaustion_emits_output_limit_failure() -> None:
    """无 provider exception 的长度恢复耗尽也发一次 output_limit 终态事件。"""
    bus = RecordingEventBus()
    agent = object.__new__(Agent)
    agent.llm = IdentityNormalizer()
    agent.agent_type = "main"
    agent.uuid = uuid.uuid4()
    agent.deps = type("Deps", (), {"event_bus": bus})()
    response = LLMResponse(
        content="unfinished",
        tool_calls={},
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "unfinished"},
    )
    ctx = RunContext(
        messages=[],
        response=response,
        length_recoveries=3,
        max_length_recoveries=3,
        response_recovery_response_count=3,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert state is AgentState.LLM_FAILURE
    assert len(failures) == 1
    failure = failures[0]
    assert failure.error_kind == LLMErrorKind.OUTPUT_LIMIT.value
    assert failure.attempts == 4
    assert failure.partial is True
    assert failure.tool_fragment_state == "none"
    assert failure.diagnostic_id.startswith("llm_")
    assert failure.caller_uuid == str(agent.uuid)


def test_length_recovery_failure_uses_response_partial_data_marker() -> None:
    """无正文和工具时，长度终态仍消费 provider 保存的思考残片标记。"""
    bus = RecordingEventBus()
    agent = object.__new__(Agent)
    agent.llm = IdentityNormalizer()
    agent.agent_type = "main"
    agent.uuid = uuid.uuid4()
    agent.deps = type("Deps", (), {"event_bus": bus})()
    response = LLMResponse(
        content="",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": ""},
    )
    response.has_partial_data = True
    ctx = RunContext(
        messages=[],
        response=response,
        length_recoveries=3,
        max_length_recoveries=3,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    failure = next(event for event in bus.events if isinstance(event, LLMCallFailed))
    assert state is AgentState.LLM_FAILURE
    assert failure.partial is True


def test_length_failure_event_publish_error_does_not_escape_handler() -> None:
    """长度恢复耗尽的 Failed 遥测故障不覆盖 LLM_FAILURE 状态。"""
    bus = FaultingEventBus(LLMCallFailed)
    agent = object.__new__(Agent)
    agent.llm = IdentityNormalizer()
    agent.agent_type = "main"
    agent.uuid = uuid.uuid4()
    agent.deps = type("Deps", (), {"event_bus": bus})()
    ctx = RunContext(
        messages=[],
        response=LLMResponse(
            content="unfinished",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "unfinished"},
        ),
        length_recoveries=3,
        max_length_recoveries=3,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_FAILURE
    assert len([event for event in bus.events if isinstance(event, LLMCallFailed)]) == 1
