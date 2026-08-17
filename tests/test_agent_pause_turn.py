"""Agent 协议续接与 pause_turn 状态机的回归测试。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext
from src.events.types import (
    AgentStateChanged,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMRetrying,
)
from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.anthropic import AnthropicProvider
from src.llm.errors import LLMErrorKind, LLMStreamResponseError


class PauseNormalizer:
    """记录 pause_turn 上限查询与消息归一化的测试 LLM。"""

    reasoning_effort = "max"

    def __init__(self, limit: int = 3) -> None:
        """初始化协议续接上限。

        Args:
            limit: pause_turn 最大自动续接次数。

        Returns:
            None。
        """
        self.limit = limit
        self.finish_reasons: list[str] = []

    def next_lower_effort(self, current: str) -> str | None:
        """返回比当前档位更低的推理力度档位。

        Args:
            current: 当前推理力度档位。

        Returns:
            下一更低档位；本测试固定无更低档位，恒为 None。
        """
        del current
        return None

    def protocol_continuation_limit(self, finish_reason: str) -> int:
        """返回 pause_turn 续接上限并记录查询终态。

        Args:
            finish_reason: 待查询的协议终态。

        Returns:
            配置的续接上限。
        """
        self.finish_reasons.append(finish_reason)
        return self.limit

    def normalize_messages(self, messages: list[dict]) -> list[dict]:
        """复制待续接消息。

        Args:
            messages: 待归一化消息。

        Returns:
            新的消息列表。
        """
        return list(messages)


class RecordingEventBus:
    """按顺序记录 Agent 遥测事件的测试总线。"""

    def __init__(self) -> None:
        """初始化空事件列表。

        Returns:
            None。
        """
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        """记录一个事件。

        Args:
            event: 待记录事件。

        Returns:
            None。
        """
        self.events.append(event)


class ProtocolScriptedProvider(LLMProvider):
    """使用真实 LLMProvider 调用模板逐次返回协议响应的测试 provider。"""

    _EFFORT_DOWNGRADE = {"max": "high", "high": "medium", "medium": "low"}

    def __init__(
        self,
        event_bus: RecordingEventBus,
        outcomes: list[LLMResponse | BaseException],
        *,
        pause_turn_limit: int = 3,
        max_attempts: int = 1,
    ) -> None:
        """初始化响应脚本与 pause_turn 上限。

        Args:
            event_bus: 记录真实调用边界事件的总线。
            outcomes: 每次请求依次消费的响应或异常。
            pause_turn_limit: pause_turn 最大自动续接次数。
            max_attempts: 每次请求的网络重试最大尝试次数。

        Returns:
            None。
        """
        super().__init__(
            api_key="",
            base_url="",
            model="protocol-stub",
            event_bus=event_bus,  # type: ignore[arg-type]
            max_attempts=max_attempts,
            base_delay_seconds=0.01,
            max_delay_seconds=0.01,
            context_limit=1000,
        )
        self.outcomes = list(outcomes)
        self.pause_turn_limit = pause_turn_limit
        self.requests: list[dict[str, Any]] = []

    async def _sleep(self, delay: float) -> None:
        """跳过真实退避等待。

        Args:
            delay: 本次退避秒数。

        Returns:
            None。
        """
        del delay

    def protocol_continuation_limit(self, finish_reason: str) -> int:
        """返回 pause_turn 自动续接上限。

        Args:
            finish_reason: provider 归一化后的终态原因。

        Returns:
            pause_turn 的配置上限，其他终态为 0。
        """
        return self.pause_turn_limit if finish_reason == "pause_turn" else 0

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

    def _normalize_assistant_extra(
        self,
        msg: dict,
        norm_msg: dict,
        role: str,
    ) -> None:
        """保留测试载体中的 Anthropic 原始 blocks。

        Args:
            msg: 归一化前的消息。
            norm_msg: 待补充的归一化消息。
            role: 已归一化的消息角色。

        Returns:
            None。
        """
        if role == "assistant" and msg.get("_anthropic_content"):
            norm_msg["_anthropic_content"] = msg["_anthropic_content"]

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
        """记录真实请求参数并消费下一项脚本结果。

        Args:
            messages: 本次会话消息。
            prompt: 本次系统提示词。
            tools: 本次工具 schema。
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
        del temperature, tool_choice, enable_thinking, call
        self.requests.append({
            "messages": deepcopy(messages),
            "prompt": deepcopy(prompt),
            "tools": deepcopy(tools),
            "reasoning_effort_override": reasoning_effort_override,
        })
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def clear_reasoning_content(self, messages: object) -> None:
        """保持测试历史不变。

        Args:
            messages: 本轮新增历史消息。

        Returns:
            None。
        """
        del messages


class StaticPromptMgr:
    """返回固定系统提示词的测试 prompt manager。"""

    def build(self) -> list[dict]:
        """构建固定系统提示词。

        Returns:
            固定系统提示词。
        """
        return [{"role": "system", "content": "固定提示"}]


class NoopReminder:
    """不生成轮次提示或工具后提醒的测试 reminder。"""

    def build_turn_start_instructions(self, mode: object, is_subagent: bool) -> str:
        """返回空轮次提示。

        Args:
            mode: 当前权限模式。
            is_subagent: 调用方是否为子智能体。

        Returns:
            空字符串。
        """
        del mode, is_subagent
        return ""

    def collect_post_round_messages(self, mode: object, is_subagent: bool) -> list[dict]:
        """返回空工具后提醒列表。

        Args:
            mode: 当前权限模式。
            is_subagent: 调用方是否为子智能体。

        Returns:
            空列表。
        """
        del mode, is_subagent
        return []


class RecordingStopHooks:
    """记录 Stop hook 收到的完整正文。"""

    def __init__(self) -> None:
        """初始化空正文记录。

        Returns:
            None。
        """
        self.final_texts: list[str] = []

    async def run_event(
        self,
        event: str,
        content: str,
        context: dict,
        **kwargs: object,
    ) -> SimpleNamespace:
        """记录 Stop hook 正文并返回未阻断结果。

        Args:
            event: hook 事件名称。
            content: 传给 hook 的正文。
            context: hook 结构化上下文。
            **kwargs: 会话与 agent 身份参数。

        Returns:
            blocked 为 False 的结果对象。
        """
        del context, kwargs
        if event == "Stop":
            self.final_texts.append(content)
        return SimpleNamespace(blocked=False)


def _runtime_agent(
    llm: LLMProvider,
    event_bus: RecordingEventBus,
    *,
    hooks_mgr: RecordingStopHooks | None = None,
) -> Agent:
    """构造覆盖协议续接状态机所需字段的最小 Agent。

    Args:
        llm: 使用真实调用模板的测试 provider。
        event_bus: Agent 与 provider 共用的事件总线。
        hooks_mgr: 可选 Stop hook 记录器。

    Returns:
        未运行 __post_init__ 的最小 Agent。
    """
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.description = ""
    agent.history = []
    agent.llm = llm
    agent._tools_schemas = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "查询",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.enable_thinking = True
    agent.plan_active = False
    agent.is_subagent = False
    agent._pending_input = ""
    agent._prompt_mgr = StaticPromptMgr()
    agent._compact_mgr = SimpleNamespace(auto_compact_size=0)
    agent._reminder_mgr = NoopReminder()
    agent.deps = SimpleNamespace(
        event_bus=event_bus,
        hooks_mgr=hooks_mgr,
        session_mgr=None,
        session_id="session-test",
    )
    agent._handlers = {
        AgentState.CHECK_COMPACT: agent._on_check_compact,
        AgentState.LLM_CALL: agent._on_llm_call,
        AgentState.PROCESS_RESPONSE: agent._on_process_response,
        AgentState.LENGTH_RETRY: agent._on_length_retry,
        AgentState.PAUSE_TURN: agent._on_pause_turn,
        AgentState.CHECK_STOP: agent._on_check_stop,
        AgentState.CONTEXT_OVERFLOW: agent._on_context_overflow,
        AgentState.LLM_FAILURE: agent._on_llm_failure,
    }
    return agent


def test_pause_turn_state_is_declared() -> None:
    """Agent 状态枚举包含独立的 pause_turn 状态。"""
    assert "pause_turn" in {state.value for state in AgentState}


def test_run_context_uses_generic_response_recovery_state() -> None:
    """运行上下文以通用 checkpoint 和 pause_turn 字段记录恢复链。"""
    ctx = RunContext(messages=[])

    assert getattr(ctx, "response_recovery_start_idx", "missing") is None
    assert getattr(ctx, "response_recovery_response_count", "missing") == 0
    assert getattr(ctx, "pause_turn_message_idx", "missing") is None
    assert getattr(ctx, "pause_turn_continuations", "missing") == 0
    assert getattr(ctx, "turn_start_messages", "missing") is None
    assert not hasattr(ctx, "length_recovery_start_idx")


def test_process_response_routes_pause_turn_without_committing_carrier() -> None:
    """pause_turn 响应先进入专用状态且不提前写入原始载体。"""
    agent = object.__new__(Agent)
    original_messages = [{"role": "user", "content": "执行服务端工具"}]
    ctx = RunContext(
        messages=list(original_messages),
        response=LLMResponse(
            content="第一段",
            finish_reason="pause_turn",
            assistant_message={
                "role": "assistant",
                "content": "第一段",
                "_anthropic_content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "server_tool_use", "id": "srv-1", "name": "web_search"},
                ],
            },
        ),
    )

    state = asyncio.run(agent._on_process_response(ctx))

    assert state is AgentState.PAUSE_TURN
    assert ctx.messages == original_messages
    assert ctx.final_text == "第一段"


def test_single_pause_appends_raw_carrier_without_synthetic_user() -> None:
    """首次 pause_turn 续接追加原始载体且不注入合成 user。"""
    agent = object.__new__(Agent)
    agent.llm = PauseNormalizer(limit=2)
    carrier = {
        "role": "assistant",
        "content": "第一段",
        "_anthropic_content": [
            {"type": "text", "text": "第一段"},
            {"type": "server_tool_use", "id": "srv-1", "name": "web_search"},
        ],
    }
    ctx = RunContext(
        messages=[{"role": "user", "content": "执行服务端工具"}],
        response=LLMResponse(
            content="第一段",
            finish_reason="pause_turn",
            assistant_message=carrier,
        ),
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    handler = getattr(agent, "_on_pause_turn", None)

    assert handler is not None
    state = asyncio.run(handler(ctx))

    assert state is AgentState.LLM_CALL
    assert ctx.messages == [
        {"role": "user", "content": "执行服务端工具"},
        carrier,
    ]
    assert ctx.response_recovery_start_idx == 1
    assert ctx.pause_turn_message_idx == 1
    assert ctx.pause_turn_continuations == 1


def test_pause_handler_does_not_keep_index_removed_by_real_normalizer() -> None:
    """真实 Anthropic 归一化删除空载体后不得保留越界索引。

    Returns:
        None。
    """
    provider = object.__new__(AnthropicProvider)
    provider.max_pause_turn_continuations = 3
    agent = object.__new__(Agent)
    agent.llm = provider
    ctx = RunContext(
        messages=[{"role": "user", "content": "继续服务端工具"}],
        response=LLMResponse(
            content="",
            finish_reason="pause_turn",
            assistant_message={
                "role": "assistant",
                "content": None,
                "_anthropic_content": [],
            },
        ),
    )

    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    assert ctx.messages == [{"role": "user", "content": "继续服务端工具"}]
    assert ctx.pause_turn_message_idx is None

    ctx.response = LLMResponse(
        content="",
        finish_reason="pause_turn",
        assistant_message={
            "role": "assistant",
            "content": None,
            "_anthropic_content": [],
        },
    )
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    assert ctx.pause_turn_message_idx is None


def test_consecutive_pause_replaces_previous_carrier_and_accumulates_text() -> None:
    """连续 pause_turn 用最新载体替换旧载体并拼接可见正文。"""
    agent = object.__new__(Agent)
    agent.llm = PauseNormalizer(limit=3)
    first_carrier = {
        "role": "assistant",
        "content": "第一段",
        "_anthropic_content": [{"type": "server_tool_use", "id": "srv-1"}],
    }
    second_carrier = {
        "role": "assistant",
        "content": "第二段",
        "_anthropic_content": [{"type": "server_tool_use", "id": "srv-2"}],
    }
    ctx = RunContext(
        messages=[{"role": "user", "content": "执行服务端工具"}],
        response=LLMResponse(
            content="第一段",
            finish_reason="pause_turn",
            assistant_message=first_carrier,
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第二段",
        finish_reason="pause_turn",
        assistant_message=second_carrier,
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN

    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    assert ctx.messages == [
        {"role": "user", "content": "执行服务端工具"},
        second_carrier,
    ]
    assert ctx.final_text == "第一段第二段"
    assert ctx.response_recovery_start_idx == 1
    assert ctx.pause_turn_message_idx == 1
    assert ctx.pause_turn_continuations == 2


def test_pause_limit_rolls_back_chain_and_emits_one_output_limit_failure() -> None:
    """pause_turn 达到协议上限时整体回滚并只发一次不可重试失败事件。"""
    bus = RecordingEventBus()
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.llm = PauseNormalizer(limit=1)
    agent.deps = SimpleNamespace(event_bus=bus)
    original_messages = [
        {"role": "user", "content": "先前问题"},
        {"role": "assistant", "content": "先前答案"},
        {"role": "user", "content": "执行服务端工具"},
    ]
    ctx = RunContext(
        messages=list(original_messages),
        response=LLMResponse(
            content="第一段",
            finish_reason="pause_turn",
            assistant_message={"role": "assistant", "content": "第一段"},
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第二段",
        finish_reason="pause_turn",
        assistant_message={"role": "assistant", "content": "第二段"},
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN

    state = asyncio.run(agent._on_pause_turn(ctx))

    assert state is AgentState.LLM_FAILURE
    assert ctx.messages == original_messages
    assert ctx.response_recovery_start_idx is None
    assert ctx.response_recovery_response_count == 0
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0
    assert ctx.llm_error is not None
    assert ctx.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert ctx.llm_error.retryable is False
    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert len(failures) == 1
    assert failures[0].error_kind == LLMErrorKind.OUTPUT_LIMIT.value
    assert failures[0].attempts == 2
    assert failures[0].partial is True
    assert failures[0].tool_fragment_state == "none"
    assert agent.llm.finish_reasons == ["pause_turn", "pause_turn"]


def test_pause_then_stop_commits_full_text_and_clears_recovery_state() -> None:
    """pause_turn 续接成功时提交完整正文、真实 assistant 并清空恢复状态。"""
    agent = object.__new__(Agent)
    agent.llm = PauseNormalizer(limit=2)
    pause_carrier = {"role": "assistant", "content": "前半句"}
    terminal_carrier = {"role": "assistant", "content": "后半句"}
    ctx = RunContext(
        messages=[{"role": "user", "content": "继续完成"}],
        response=LLMResponse(
            content="前半句",
            finish_reason="pause_turn",
            assistant_message=pause_carrier,
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="后半句",
        finish_reason="stop",
        assistant_message=terminal_carrier,
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    assert ctx.final_text == "前半句后半句"
    assert ctx.messages == [
        {"role": "user", "content": "继续完成"},
        pause_carrier,
        terminal_carrier,
    ]
    assert ctx.response_recovery_start_idx is None
    assert ctx.response_recovery_response_count == 0
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0


def test_pause_then_length_keeps_checkpoint_but_clears_pause_carrier_index() -> None:
    """pause_turn 转 length 时保留整链 checkpoint 并停止替换旧 pause 载体。"""
    agent = object.__new__(Agent)
    agent.llm = PauseNormalizer(limit=3)
    ctx = RunContext(
        messages=[{"role": "user", "content": "完成混合续接"}],
        response=LLMResponse(
            content="第一段",
            finish_reason="pause_turn",
            assistant_message={"role": "assistant", "content": "第一段"},
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第二段",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "第二段"},
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.LENGTH_RETRY

    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    assert ctx.response_recovery_start_idx == 1
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 1
    assert ctx.final_text == "第一段第二段"
    assert [message["role"] for message in ctx.messages] == [
        "user",
        "assistant",
        "assistant",
        "user",
    ]

    new_pause_carrier = {"role": "assistant", "content": "第三段"}
    ctx.response = LLMResponse(
        content="第三段",
        finish_reason="pause_turn",
        assistant_message=new_pause_carrier,
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.PAUSE_TURN
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_CALL
    assert ctx.messages[-1] is new_pause_carrier
    assert ctx.messages[1]["content"] == "第一段"
    assert ctx.pause_turn_message_idx == len(ctx.messages) - 1
    assert ctx.pause_turn_continuations == 2
    assert ctx.final_text == "第一段第二段第三段"


def test_protocol_continuations_preserve_request_and_emit_independent_usage() -> None:
    """连续 pause_turn 保持 prompt/tools，替换载体并逐次发出调用与 usage 事件。"""
    bus = RecordingEventBus()
    hooks = RecordingStopHooks()
    first_carrier = {
        "role": "assistant",
        "content": "前半句",
        "_anthropic_content": [
            {"type": "text", "text": "前半句"},
            {"type": "server_tool_use", "id": "srv-1", "name": "web_search"},
        ],
    }
    second_carrier = {
        "role": "assistant",
        "content": "中间",
        "_anthropic_content": [
            {"type": "text", "text": "中间"},
            {"type": "server_tool_use", "id": "srv-2", "name": "web_search"},
        ],
    }
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="前半句",
                finish_reason="pause_turn",
                assistant_message=first_carrier,
                token_usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            ),
            LLMResponse(
                content="中间",
                finish_reason="pause_turn",
                assistant_message=second_carrier,
                token_usage={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            ),
            LLMResponse(
                content="后半句",
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "后半句"},
                token_usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            ),
        ],
        pause_turn_limit=3,
    )
    agent = _runtime_agent(provider, bus, hooks_mgr=hooks)

    result = asyncio.run(agent.run("执行服务端工具"))

    assert result.final_text == "前半句中间后半句"
    assert hooks.final_texts == ["前半句中间后半句"]
    assert len(provider.requests) == 3
    assert provider.requests[1]["messages"] == [
        {"role": "user", "content": "执行服务端工具"},
        first_carrier,
    ]
    assert provider.requests[2]["messages"] == [
        {"role": "user", "content": "执行服务端工具"},
        second_carrier,
    ]
    assert all(
        request["prompt"] == provider.requests[0]["prompt"]
        for request in provider.requests
    )
    assert all(
        request["tools"] == provider.requests[0]["tools"]
        for request in provider.requests
    )
    assert agent.history == [
        {"role": "user", "content": "执行服务端工具"},
        second_carrier,
        {"role": "assistant", "content": "后半句"},
    ]
    starts = [event for event in bus.events if isinstance(event, LLMCallStarted)]
    completed = [event for event in bus.events if isinstance(event, LLMCallCompleted)]
    assert len(starts) == 3
    assert len(completed) == 3
    assert [
        (event.input_tokens, event.output_tokens, event.total_tokens)
        for event in completed
    ] == [(10, 2, 12), (11, 3, 14), (12, 4, 16)]
    transitions = [
        (event.from_state, event.to_state)
        for event in bus.events
        if isinstance(event, AgentStateChanged)
    ]
    assert transitions.count(("process_response", "pause_turn")) == 2
    assert transitions.count(("pause_turn", "llm_call")) == 2


def test_network_retry_attempts_do_not_consume_pause_continuations() -> None:
    """请求内网络重试与 pause_turn 协议续接分别计数。"""
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMStreamResponseError("服务暂时不可用", code="server_error"),
            LLMResponse(
                content="前半句",
                finish_reason="pause_turn",
                assistant_message={"role": "assistant", "content": "前半句"},
            ),
            LLMResponse(
                content="后半句",
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "后半句"},
            ),
        ],
        pause_turn_limit=1,
        max_attempts=2,
    )
    agent = _runtime_agent(provider, bus)

    result = asyncio.run(agent.run("先重试再续接"))

    assert result.final_text == "前半句后半句"
    starts = [event for event in bus.events if isinstance(event, LLMCallStarted)]
    completed = [event for event in bus.events if isinstance(event, LLMCallCompleted)]
    retries = [event for event in bus.events if isinstance(event, LLMRetrying)]
    assert [event.attempt for event in starts] == [1, 2, 1]
    assert len(completed) == 2
    assert len(retries) == 1
    assert not any(isinstance(event, LLMCallFailed) for event in bus.events)


def test_length_three_segments_reach_run_result_and_stop_hook() -> None:
    """三段 length 恢复把完整正文交给 RunResult 与 Stop hook。"""
    bus = RecordingEventBus()
    hooks = RecordingStopHooks()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="前半句",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "前半句"},
            ),
            LLMResponse(
                content="中间",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "中间"},
            ),
            LLMResponse(
                content="后半句",
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "后半句"},
            ),
        ],
    )
    agent = _runtime_agent(provider, bus, hooks_mgr=hooks)

    result = asyncio.run(agent.run("继续写完"))

    assert result.final_text == "前半句中间后半句"
    assert hooks.final_texts == ["前半句中间后半句"]


def test_pause_continuation_provider_failure_rolls_back_without_duplicate_event() -> None:
    """pause_turn 续接终态错误回滚整链且不重复 provider 的失败事件。"""
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="已显示残片",
                finish_reason="pause_turn",
                assistant_message={
                    "role": "assistant",
                    "content": "已显示残片",
                    "_anthropic_content": [
                        {"type": "server_tool_use", "id": "srv-1"},
                    ],
                },
            ),
            LLMStreamResponseError("响应被拒绝", code="content_filter"),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)

    result = asyncio.run(agent.run("执行服务端工具"))

    assert result.llm_error is not None
    assert result.llm_error.kind is LLMErrorKind.CONTENT_POLICY
    assert agent.history == [{"role": "user", "content": "执行服务端工具"}]
    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert len(failures) == 1
    assert failures[0].error_kind == LLMErrorKind.CONTENT_POLICY.value
    transitions = [
        (event.from_state, event.to_state)
        for event in bus.events
        if isinstance(event, AgentStateChanged)
    ]
    assert ("llm_call", "llm_failure") in transitions


def test_pause_length_stop_mixed_chain_commits_history_and_clears_state() -> None:
    """pause_turn→length→stop 混合链拼接正文并正确提交历史与恢复状态。"""
    bus = RecordingEventBus()
    pause_carrier = {
        "role": "assistant",
        "content": "第一段",
        "_anthropic_content": [{"type": "server_tool_use", "id": "srv-1"}],
    }
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="第一段",
                finish_reason="pause_turn",
                assistant_message=pause_carrier,
            ),
            LLMResponse(
                content="第二段",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "第二段"},
            ),
            LLMResponse(
                content="第三段",
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "第三段"},
            ),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)
    agent.history.append({"role": "user", "content": "执行混合续接"})
    ctx = RunContext(
        messages=agent.history,
        round_start_idx=0,
        user_input="执行混合续接",
    )

    result = asyncio.run(agent._run_single_turn(ctx, AgentState.CHECK_COMPACT))

    assert result.final_text == "第一段第二段第三段"
    assert agent.history == [
        {"role": "user", "content": "执行混合续接"},
        pause_carrier,
        {"role": "assistant", "content": "第二段"},
        {
            "role": "user",
            "content": "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。",
        },
        {"role": "assistant", "content": "第三段"},
    ]
    assert ctx.response_recovery_start_idx is None
    assert ctx.response_recovery_response_count == 0
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0
    assert ctx.length_recoveries == 1


def test_thinking_content_pause_mixed_chain_persists_effort_until_clean_terminal() -> None:
    """思考→正文→pause_turn→stop 混合链：降档 effort 跨腿持续，干净终态才复位。"""
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="",
                finish_reason="length",
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "被丢弃的半截推理",
                },
            ),
            LLMResponse(
                content="正文段",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "正文段"},
            ),
            LLMResponse(
                content="暂停段",
                finish_reason="pause_turn",
                assistant_message={"role": "assistant", "content": "暂停段"},
            ),
            LLMResponse(
                content="收尾段",
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "收尾段"},
            ),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)
    agent.history.append({"role": "user", "content": "执行混合恢复"})
    ctx = RunContext(messages=agent.history, round_start_idx=0, user_input="执行混合恢复")

    result = asyncio.run(agent._run_single_turn(ctx, AgentState.CHECK_COMPACT))

    # 思考腿被丢弃、不贡献正文，正文/暂停/收尾三段依次拼接。
    assert result.final_text == "正文段暂停段收尾段"
    # 思考腿与正文腿各计一次长度恢复；干净终态复位降档与压缩瞬态。
    assert ctx.length_recoveries == 2
    assert ctx.length_effort_override is None
    assert ctx.length_ephemeral_instruction is None
    assert ctx.response_recovery_start_idx is None
    assert ctx.pause_turn_continuations == 0
    # 思考腿不写历史；正文腿保留 assistant 与续写 user；暂停/收尾正常提交。
    assert agent.history == [
        {"role": "user", "content": "执行混合恢复"},
        {"role": "assistant", "content": "正文段"},
        {
            "role": "user",
            "content": "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。",
        },
        {"role": "assistant", "content": "暂停段"},
        {"role": "assistant", "content": "收尾段"},
    ]
    # 首次请求用初始档，思考腿降档后 high 持续到收尾腿（干净终态才复位）。
    efforts = [request["reasoning_effort_override"] for request in provider.requests]
    assert efforts == [None, "high", "high", "high"]


def test_pause_length_provider_failure_rolls_back_entire_mixed_chain() -> None:
    """pause_turn→length 后的 provider 终态错误回滚全部混合恢复消息。"""
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="第一段",
                finish_reason="pause_turn",
                assistant_message={"role": "assistant", "content": "第一段"},
            ),
            LLMResponse(
                content="第二段",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "第二段"},
            ),
            LLMStreamResponseError("响应被拒绝", code="content_filter"),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)
    original_messages = [{"role": "user", "content": "执行混合续接"}]
    agent.history.extend(original_messages)
    ctx = RunContext(messages=agent.history, round_start_idx=0)

    result = asyncio.run(agent._run_single_turn(ctx, AgentState.CHECK_COMPACT))

    assert result.llm_error is not None
    assert result.llm_error.kind is LLMErrorKind.CONTENT_POLICY
    assert agent.history == original_messages
    assert ctx.response_recovery_start_idx is None
    assert ctx.response_recovery_response_count == 0
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0
    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert len(failures) == 1


def test_pause_length_exhaustion_rolls_back_entire_mixed_chain() -> None:
    """pause_turn→length 在长度恢复耗尽时回滚全部混合恢复消息。"""
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="第一段",
                finish_reason="pause_turn",
                assistant_message={"role": "assistant", "content": "第一段"},
            ),
            LLMResponse(
                content="第二段",
                finish_reason="length",
                assistant_message={"role": "assistant", "content": "第二段"},
            ),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)
    original_messages = [{"role": "user", "content": "执行混合续接"}]
    agent.history.extend(original_messages)
    ctx = RunContext(
        messages=agent.history,
        round_start_idx=0,
        max_length_recoveries=0,
    )

    result = asyncio.run(agent._run_single_turn(ctx, AgentState.CHECK_COMPACT))

    assert result.llm_error is not None
    assert result.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert result.llm_error.retryable is False
    assert agent.history == original_messages
    assert ctx.response_recovery_start_idx is None
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0
    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert len(failures) == 1
    assert failures[0].error_kind == LLMErrorKind.OUTPUT_LIMIT.value
    assert failures[0].attempts == 2


def test_length_pause_exhaustion_counts_both_recovery_responses() -> None:
    """length→pause_turn 耗尽时失败事件统计完整混合恢复链。

    Returns:
        None。
    """
    bus = RecordingEventBus()
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.llm = PauseNormalizer(limit=0)
    agent.deps = SimpleNamespace(event_bus=bus)
    original_messages = [{"role": "user", "content": "执行反向混合续接"}]
    ctx = RunContext(
        messages=list(original_messages),
        response=LLMResponse(
            content="第一段",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "第一段"},
        ),
    )

    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第二段",
        finish_reason="pause_turn",
        assistant_message={"role": "assistant", "content": "第二段"},
    )
    assert asyncio.run(agent._on_pause_turn(ctx)) is AgentState.LLM_FAILURE

    failures = [event for event in bus.events if isinstance(event, LLMCallFailed)]
    assert ctx.messages == original_messages
    assert len(failures) == 1
    assert failures[0].attempts == 2


def test_new_recovery_chain_attempts_ignore_prior_length_recoveries() -> None:
    """已收口链的 length 计数不得污染新链失败事件。

    Returns:
        None。
    """
    bus = RecordingEventBus()
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.llm = PauseNormalizer(limit=2)
    agent.deps = SimpleNamespace(event_bus=bus)
    completed_assistant = {"role": "assistant", "content": "第一条已完成"}
    ctx = RunContext(
        messages=[{"role": "user", "content": "分两条处理"}],
        response=LLMResponse(
            content="第一条前半段",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "第一条前半段"},
        ),
        max_length_recoveries=1,
    )

    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第一条已完成",
        finish_reason="stop",
        assistant_message=completed_assistant,
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    assert ctx.response_recovery_response_count == 0

    ctx.response = LLMResponse(
        content="第二条仍被截断",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "第二条仍被截断"},
    )
    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_FAILURE

    failure = next(event for event in bus.events if isinstance(event, LLMCallFailed))
    assert failure.attempts == 1


def test_cancel_after_compact_restores_turn_start_history_and_recovery_state() -> None:
    """compact 改写历史后的取消应保留本轮消息并补中断标记。

    Returns:
        None。
    """
    bus = RecordingEventBus()
    provider = ProtocolScriptedProvider(bus, [])
    agent = _runtime_agent(provider, bus)
    turn_start_messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧答案"},
    ]
    agent.history = [
        {"role": "user", "content": "压缩摘要"},
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "临时 pause 载体"},
    ]
    ctx = RunContext(
        messages=agent.history,
        round_start_idx=100,
        turn_start_messages=list(turn_start_messages),
        user_input="当前问题",
        response_recovery_start_idx=2,
        response_recovery_response_count=1,
        pause_turn_message_idx=2,
        pause_turn_continuations=1,
    )

    async def cancel_call(current: RunContext) -> AgentState:
        """模拟协议续接调用被取消。

        Args:
            current: 当前运行上下文。

        Returns:
            不返回。

        Raises:
            asyncio.CancelledError: 固定取消异常。
        """
        del current
        raise asyncio.CancelledError("cancel continuation")

    agent._handlers[AgentState.LLM_CALL] = cancel_call

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent._run_single_turn(ctx, AgentState.LLM_CALL))

    # 中断保留历史（不回滚）：回滚恢复链后到干净尾部 [压缩摘要, 当前问题]，
    # 再补一条中断标记 assistant 消息。
    assert agent.history == [
        {"role": "user", "content": "压缩摘要"},
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "⏸ 本轮已被用户中断。"},
    ]
    assert agent._pending_input == "当前问题"
    assert ctx.response_recovery_start_idx is None
    assert ctx.response_recovery_response_count == 0
    assert ctx.pause_turn_message_idx is None
    assert ctx.pause_turn_continuations == 0


def test_subagent_run_restores_automatic_snapshot_after_compact_pause_cancel() -> None:
    """直接输入路径在 compact 与 pause 取消后保留本轮消息并补中断标记。

    Returns:
        None。
    """
    bus = RecordingEventBus()
    pause_carrier = {
        "role": "assistant",
        "content": "临时结果",
        "_anthropic_content": [{"type": "server_tool_use", "id": "srv-1"}],
    }
    provider = ProtocolScriptedProvider(
        bus,
        [
            LLMResponse(
                content="临时结果",
                finish_reason="pause_turn",
                assistant_message=pause_carrier,
            ),
            asyncio.CancelledError("cancel after compact pause"),
        ],
        pause_turn_limit=2,
    )
    agent = _runtime_agent(provider, bus)
    turn_start_messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧答案"},
    ]
    agent.history.extend(turn_start_messages)

    class RewritingCompact:
        """首次调用时重写历史并关闭后续自动 compact。"""

        auto_compact_size = 1

        def is_need_compact(
            self,
            messages: list[dict],
            prompt: list[dict] | None,
            tools: list[dict] | None = None,
            estimated_tokens: int | None = None,
        ) -> bool:
            """按测试阈值判断是否需要 compact。

            Args:
                messages: 当前会话消息。
                prompt: 当前系统提示词。
                tools: 当前工具 schema。
                estimated_tokens: 已计算的输入 token 数。

            Returns:
                输入 token 数超过当前阈值时返回 True。
            """
            del messages, prompt, tools
            return bool(
                estimated_tokens is not None
                and estimated_tokens > self.auto_compact_size
            )

        async def compact_history(self, messages: list[dict]) -> SimpleNamespace:
            """用摘要和当前用户消息替换历史。

            Args:
                messages: compact 前的完整消息。

            Returns:
                可供 Agent 消费的 compact 结果。
            """
            self.auto_compact_size = 10

            def estimate_compacted_tokens(
                compacted_messages: list[dict],
                prompt: list[dict] | None = None,
                tools: list[dict] | None = None,
            ) -> int:
                """返回 compact 后降低的输入 token 数。

                Args:
                    compacted_messages: compact 后消息。
                    prompt: 当前系统提示词。
                    tools: 当前工具 schema。

                Returns:
                    固定值 6。
                """
                del compacted_messages, prompt, tools
                return 6

            provider.estimate_tokens = estimate_compacted_tokens
            return SimpleNamespace(
                messages=[
                    {"role": "user", "content": "压缩摘要"},
                    messages[-1],
                ],
                transcript_path=None,
                summarized_message_count=2,
                summary="摘要",
            )

    agent._compact_mgr = RewritingCompact()
    agent._handlers[AgentState.COMPACT] = agent._on_compact

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.run("当前问题"))

    assert len(provider.requests) == 2
    assert provider.requests[1]["messages"][-1] == pause_carrier
    # 中断保留历史（不回滚）：compact 改写后历史为 [压缩摘要, 当前问题]，
    # 取消时回滚恢复链移除 pause 载体、补中断标记 assistant 消息。
    assert agent.history == [
        {"role": "user", "content": "压缩摘要"},
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "⏸ 本轮已被用户中断。"},
    ]
    assert agent._pending_input == "当前问题"
