"""长度截断分类与按调用推理力度 override 的回归测试。"""

from __future__ import annotations

import asyncio

from src.llm.base import (
    LLMCallContext,
    LLMProvider,
    LLMResponse,
    TruncationKind,
    _has_reasoning_carrier,
    classify_truncation,
)


class _StubEventBus:
    """吞掉全部事件的测试总线。"""

    async def emit(self, event: object) -> None:
        """忽略一个事件。

        Args:
            event: 待发布事件。

        Returns:
            None。
        """
        del event


class RecordingProvider(LLMProvider):
    """记录 _do_chat 收到的 effort 与消息并返回预置响应的测试 provider。"""

    _EFFORT_DOWNGRADE = {"max": "high", "high": "medium"}

    def __init__(self, response: LLMResponse) -> None:
        """以预置响应初始化 provider。

        Args:
            response: 每次 _do_chat 返回的固定响应。

        Returns:
            None。
        """
        super().__init__(
            api_key="",
            base_url="",
            model="recording-stub",
            event_bus=_StubEventBus(),  # type: ignore[arg-type]
            max_attempts=1,
            base_delay_seconds=0.01,
            max_delay_seconds=0.01,
            context_limit=1000,
        )
        self._response = response
        self.seen_efforts: list[str | None] = []
        self.seen_messages: list[list[dict]] = []

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
            固定值 1。
        """
        del messages, prompt, tools
        return 1

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
        """记录本次收到的 effort 与消息并返回预置响应。

        Args:
            messages: 本次调用的会话消息（可能含尾部一次性指令）。
            prompt: 系统提示词。
            tools: 工具 schema。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位。
            call: 当前尝试上下文。

        Returns:
            预置响应。
        """
        del prompt, tools, temperature, tool_choice, enable_thinking, call
        self.seen_efforts.append(reasoning_effort_override)
        self.seen_messages.append(messages)
        return self._response


def _length_response(content: str = "", **assistant_fields: object) -> LLMResponse:
    """构造 length 截断响应，可选注入 assistant 消息附加字段。

    Args:
        content: 可见正文。
        assistant_fields: 追加到 assistant 消息的键值（如 reasoning_content）。

    Returns:
        finish_reason 为 length 的响应。
    """
    assistant_message: dict = {"role": "assistant", "content": content}
    assistant_message.update(assistant_fields)
    return LLMResponse(
        content=content,
        finish_reason="length",
        assistant_message=assistant_message,
    )


def test_classify_tool_call_wins_over_content_and_thinking() -> None:
    """同时存在工具、正文与思考残片时归为工具调用截断。"""
    response = LLMResponse(
        content="前言",
        tool_calls={0: {"id": "c1", "name": "lookup", "arguments": "{"}},
        finish_reason="length",
        assistant_message={
            "role": "assistant",
            "content": "前言",
            "reasoning_content": "推理",
        },
    )
    assert classify_truncation(response) == TruncationKind.TOOL_CALL.value


def test_classify_partial_tool_fragment_from_call_wins() -> None:
    """仅凭 call 的半截工具片段即可判定为工具调用截断。"""
    call = LLMCallContext(attempt=1)
    call.record_tool_fragment(0, call_id="c1", name="lookup", arguments='{"q":')
    response = _length_response(content="有正文")
    assert classify_truncation(response, call) == TruncationKind.TOOL_CALL.value


def test_classify_content_wins_over_thinking() -> None:
    """有正文且有思考但无工具时归为正文截断。"""
    response = _length_response(content="半句正文", reasoning_content="推理")
    assert classify_truncation(response) == TruncationKind.CONTENT.value


def test_classify_only_thinking_is_thinking() -> None:
    """正文为空、仅有思考载体时归为思考截断。"""
    response = _length_response(content="", reasoning_content="半截推理")
    assert classify_truncation(response) == TruncationKind.THINKING.value


def test_classify_all_empty_is_unknown() -> None:
    """正文、工具、思考全空时归为未知截断。"""
    response = _length_response(content="")
    assert classify_truncation(response) == TruncationKind.UNKNOWN.value


def test_classify_thinking_from_call_thinking_parts() -> None:
    """仅 call.thinking_parts 有内容时归为思考截断。"""
    call = LLMCallContext(attempt=1)
    call.record_thinking_delta("流式思考片段")
    response = _length_response(content="")
    assert classify_truncation(response, call) == TruncationKind.THINKING.value


def test_has_reasoning_carrier_covers_all_providers() -> None:
    """四种 provider 的推理载体均被识别为思考载体。"""
    assert _has_reasoning_carrier({"reasoning_content": "x"}) is True
    assert _has_reasoning_carrier({"reasoning": "x"}) is True
    assert _has_reasoning_carrier(
        {"_anthropic_content": [{"type": "thinking", "thinking": "x"}]}
    ) is True
    assert _has_reasoning_carrier(
        {"_response_output": [{"type": "reasoning", "summary": []}]}
    ) is True
    assert _has_reasoning_carrier({"content": "无推理"}) is False
    assert _has_reasoning_carrier(None) is False


def test_has_reasoning_carrier_ignores_non_reasoning_blocks() -> None:
    """Anthropic 非 thinking 块与 Responses 非 reasoning 项不算思考载体。"""
    assert _has_reasoning_carrier(
        {"_anthropic_content": [{"type": "text", "text": "x"}]}
    ) is False
    assert _has_reasoning_carrier(
        {"_response_output": [{"type": "function_call", "arguments": "{}"}]}
    ) is False


def test_chat_sets_truncation_kind_only_on_length() -> None:
    """仅当 finish_reason 为 length 时才计算并写入 truncation_kind。"""
    stop_response = LLMResponse(
        content="完整答复",
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": "完整答复"},
    )
    provider = RecordingProvider(stop_response)
    result = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
    assert result.truncation_kind is None

    length_response = _length_response(content="", reasoning_content="半截推理")
    provider = RecordingProvider(length_response)
    result = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
    assert result.truncation_kind == TruncationKind.THINKING.value


def test_chat_threads_effort_override_without_mutating_shared_field() -> None:
    """override 传入 _do_chat 且不修改共享的 reasoning_effort；None 时回退默认。"""
    provider = RecordingProvider(
        LLMResponse(
            content="ok",
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "ok"},
        )
    )
    assert provider.reasoning_effort == "max"

    asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
    asyncio.run(
        provider.chat(
            [{"role": "user", "content": "hi"}],
            reasoning_effort_override="high",
        )
    )

    # 第一次无 override 回退默认（provider 内部用 override or self.reasoning_effort），
    # 这里断言 _do_chat 收到的原始 override 参数：None 与显式 high。
    assert provider.seen_efforts == [None, "high"]
    # 共享字段始终未被按调用 override 修改。
    assert provider.reasoning_effort == "max"


def test_chat_ephemeral_instruction_appended_without_mutating_caller_messages() -> None:
    """一次性指令作为尾部 user 传给 _do_chat，且不改动调用方消息列表。"""
    provider = RecordingProvider(
        LLMResponse(
            content="ok",
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "ok"},
        )
    )
    caller_messages = [{"role": "user", "content": "原始问题"}]

    asyncio.run(
        provider.chat(
            caller_messages,
            ephemeral_instruction="请压缩思考",
        )
    )

    # 调用方列表保持原样，未被追加一次性指令。
    assert caller_messages == [{"role": "user", "content": "原始问题"}]
    # _do_chat 收到的消息尾部才是一次性 user 指令。
    seen = provider.seen_messages[0]
    assert seen[-1] == {"role": "user", "content": "请压缩思考"}
    assert seen[0] == {"role": "user", "content": "原始问题"}


def test_next_lower_effort_walks_and_bottoms_out() -> None:
    """降档阶梯逐级下降，触底返回 None。"""
    provider = RecordingProvider(
        LLMResponse(content="", finish_reason="stop", assistant_message={})
    )
    assert provider.next_lower_effort("max") == "high"
    assert provider.next_lower_effort("high") == "medium"
    assert provider.next_lower_effort("medium") is None
    assert provider.next_lower_effort("low") is None
