"""工作期间排队输入（chat 前并入对话）的时机与边界回归测试。"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from types import SimpleNamespace

import pytest

from src.agent.agent import Agent
from src.agent.spec import AgentSpec
from src.agent.states import AgentState, RunContext
from src.common.session_state import SessionState
from src.events.types import UserInputAdopted
from src.llm.base import LLMResponse
from src.mode import RunMode


class RecordingEventBus:
    """记录事件并统计输入窗口请求的测试事件总线。"""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.input_requests = 0
        self.outputs: list[str] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)

    async def request_input(self, prompt: str) -> str:
        del prompt
        self.input_requests += 1
        raise AssertionError("排队输入不应打开输入窗口")

    async def request_output(self, content: str, **kwargs: object) -> None:
        del kwargs
        self.outputs.append(content)

    def adopted_texts(self) -> list[str]:
        """按顺序返回并入事件携带的文本。"""
        return [
            event.text for event in self.events
            if isinstance(event, UserInputAdopted)
        ]


class RecordingLLM:
    """记录每次 chat 收到的消息并返回成功响应的测试 LLM。"""

    context_limit = 1000
    reasoning_effort = "max"

    def __init__(self, *, cancel_first: bool = False) -> None:
        self.cancel_first = cancel_first
        self.requests: list[list[dict]] = []

    async def chat(self, **kwargs: object) -> LLMResponse:
        messages = kwargs.get("messages")
        recorded = list(messages) if isinstance(messages, list) else []
        self.requests.append([dict(message) for message in recorded])
        if self.cancel_first:
            self.cancel_first = False
            raise asyncio.CancelledError()
        return LLMResponse(
            content="收到",
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "收到"},
        )

    def normalize_messages(self, messages: list[dict]) -> list[dict]:
        return list(messages)

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[dict] | None = None,
    ) -> int:
        del messages, prompt, tools
        return 1


class NoopReminder:
    """不产生任何提醒的测试 reminder_manager。"""

    def queue_turn_start(self, mode: object, is_subagent: bool) -> None:
        del mode, is_subagent

    def queue_post_round(self, mode: object, is_subagent: bool) -> None:
        del mode, is_subagent

    def pop_pending(self) -> list[str]:
        return []


class StaticPromptMgr:
    """返回固定 system 与空动态段的测试 prompt_manager。"""

    def build_initial_context_messages(self) -> list[dict]:
        return []

    def build_capability_instructions(self, mode: RunMode) -> str:
        del mode
        return ""

    def build_manager_instructions(self) -> str:
        return ""

    def build_mode_instructions(self, mode: RunMode) -> str:
        del mode
        return ""


def _agent(
    llm: RecordingLLM,
    *,
    event_bus: RecordingEventBus | None = None,
    state: SessionState | None = None,
    is_subagent: bool = False,
    data_guard: object | None = None,
) -> Agent:
    """构造覆盖排队输入链路所需字段的最小 Agent。"""
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.spec = AgentSpec(
        agent_type="worker" if is_subagent else "main",
        description="",
        is_subagent=is_subagent,
    )
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent.reasoning_effort = None
    agent._pending_user_inputs = deque()
    agent.assembly = SimpleNamespace(
        llm=llm,
        model="test/model",
        prompt_mgr=StaticPromptMgr(),
        compact_mgr=SimpleNamespace(auto_compact_size=0),
        reminder_mgr=NoopReminder(),
        task_mgr=None,
        skill_mgr=None,
        subagent_mgr=None,
        system_prompt=[],
    )
    agent.deps = SimpleNamespace(
        event_bus=event_bus or RecordingEventBus(),
        hooks_mgr=None,
        session_mgr=None,
        session=SimpleNamespace(state=state, id="queued-input-test"),
        tools_mgr=SimpleNamespace(schemas=lambda: []),
        data_guard=data_guard,
    )
    agent._handlers = {
        AgentState.REQUEST_INPUT: agent._on_request_input,
        AgentState.CHECK_COMPACT: agent._on_check_compact,
        AgentState.LLM_CALL: agent._on_llm_call,
        AgentState.PROCESS_RESPONSE: agent._on_process_response,
        AgentState.CHECK_STOP: agent._on_check_stop,
    }
    return agent


def test_queued_input_is_injected_before_next_chat() -> None:
    """排队输入在下一次 chat 前作为 user 消息并入模型上下文与会话状态。"""
    bus = RecordingEventBus()
    state = SessionState()
    llm = RecordingLLM()
    agent = _agent(llm, event_bus=bus, state=state)
    assert agent.queue_user_input("补充说明：先看日志") is True

    ctx = RunContext(messages=agent.history)
    asyncio.run(agent._on_llm_call(ctx))

    assert agent.pending_user_inputs() == []
    assert bus.input_requests == 0
    assert bus.adopted_texts() == ["补充说明：先看日志"]
    assert agent.history[-1] == {"role": "user", "content": "补充说明：先看日志"}
    llm_messages = llm.requests[0]
    assert llm_messages[-1] == {"role": "user", "content": "补充说明：先看日志"}
    user_records = [record for record in state.visible_records() if record.kind == "user"]
    assert [record.view.data["text"] for record in user_records] == ["补充说明：先看日志"]


def test_queued_inputs_keep_fifo_order_and_blank_is_ignored() -> None:
    """多条排队输入按提交顺序并入；纯空白不入队。"""
    agent = _agent(RecordingLLM())
    assert agent.queue_user_input("   ") is False
    agent.queue_user_input("第一条")
    agent.queue_user_input("第二条")

    asyncio.run(agent._on_llm_call(RunContext(messages=agent.history)))

    assert [message["content"] for message in agent.history] == ["第一条", "第二条"]


def test_queued_command_defers_whole_queue_until_input_window() -> None:
    """队首是命令时整体延后：命令必须由 REQUEST_INPUT 解析，不能当提示词发出去。"""
    agent = _agent(RecordingLLM())
    agent.queue_user_input("/models")
    agent.queue_user_input("后续说明")

    asyncio.run(agent._on_llm_call(RunContext(messages=agent.history)))

    assert agent.history == []
    assert agent.pending_user_inputs() == ["/models", "后续说明"]


def test_queued_input_starts_next_input_window_without_prompting() -> None:
    """队列非空时 REQUEST_INPUT 直接消费队首，不再打开输入窗口。"""
    bus = RecordingEventBus()
    state = SessionState()
    agent = _agent(RecordingLLM(), event_bus=bus, state=state)
    agent.queue_user_input("exit")
    ctx = RunContext(messages=agent.history)

    next_state = asyncio.run(agent._on_request_input(ctx))

    assert next_state is AgentState.DONE
    assert ctx.exit_requested is True
    assert ctx.user_input == "exit"
    assert bus.input_requests == 0
    assert bus.adopted_texts() == ["exit"]
    assert agent.pending_user_inputs() == []


def test_subagent_does_not_consume_queued_input() -> None:
    """子 agent 不消费排队输入，避免把用户的话注入到子 agent 上下文。"""
    agent = _agent(RecordingLLM(), is_subagent=True)
    agent.queue_user_input("只给主 agent 的话")

    asyncio.run(agent._on_llm_call(RunContext(messages=agent.history)))

    assert agent.history == []
    assert agent.pending_user_inputs() == ["只给主 agent 的话"]


def test_cancelled_chat_keeps_injected_user_message() -> None:
    """并入发生在回滚点之前：本次调用被取消后用户消息仍留在模型历史。"""
    agent = _agent(RecordingLLM(cancel_first=True))
    agent.queue_user_input("别丢了我")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.run("原始问题"))

    assert [message.get("content") for message in agent.history] == [
        "原始问题",
        "别丢了我",
    ]

def _redacting_guard() -> SimpleNamespace:
    """构造同时处理字符串与消息字典的脱敏替身，模拟共享 DataGuard。"""

    def redact(value: object) -> object:
        if isinstance(value, str):
            return value.replace("hunter2", "***")
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    return SimpleNamespace(redact=redact)


def test_injected_input_is_redacted_before_storage() -> None:
    """并入前先脱敏：显示、内存与下一轮请求共用同一份脱敏文本。"""
    agent = _agent(RecordingLLM(), data_guard=_redacting_guard())
    agent.queue_user_input("口令 hunter2 请记下")

    asyncio.run(agent._on_llm_call(RunContext(messages=agent.history)))

    assert agent.history[-1] == {"role": "user", "content": "口令 *** 请记下"}
