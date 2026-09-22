"""Ctrl+C 中断收尾语义：按部件完整度收束上下文、补中止结果与 `<turn_aborted>` 标记。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid

from src.agent.agent import Agent
from src.agent import SessionContext
from src.agent.spec import AgentSpec
from src.agent.states import AgentState, RunContext
from src.common.session_log import fold_log, iter_batch_lines
from src.common.tool_display import ToolResult
from src.common.session_state import SessionState, ViewPayload
from src.llm.base import LLMCallContext, LLMResponse
from src.llm.anthropic import AnthropicProvider
from src.llm.deepseek import DeepSeekProvider
from src.mgr.session_mgr import SessionMgr
from src.mode import RunMode


class _StubTool:
    """仅提供 `_on_execute_tools` 需要的并行标记。"""

    def __init__(self, name: str, *, parallel: bool) -> None:
        self.name = name
        self.parallel = parallel


class _ScriptedToolsMgr:
    """按工具名阻塞或立即返回的测试工具管理器。"""

    def __init__(self, *, blocking: set[str], parallel: bool) -> None:
        """初始化工具集合。

        Args:
            blocking: 调用后永不返回的工具名。
            parallel: 所有工具声明的并行标记。
        """
        self._blocking = blocking
        self._parallel = parallel
        self._tools: dict[str, _StubTool] = {}
        self.entered = asyncio.Event()

    def get(self, name: str) -> _StubTool | None:
        """按名返回工具条目（延迟注册，避免与阻塞集合耦合）。"""
        if name not in self._tools:
            self._tools[name] = _StubTool(name, parallel=self._parallel)
        return self._tools.get(name)

    async def execute(self, name: str, arguments: object, **kwargs: object) -> ToolResult:
        """阻塞型工具永不返回，其余工具直接返回文本结果。"""
        del arguments, kwargs
        if name in self._blocking:
            self.entered.set()
            await asyncio.Event().wait()
        return ToolResult("ok")

    def schemas(self) -> list[dict]:
        """LLM 调用不需要工具 schema。"""
        return []


def _agent(tools_mgr: _ScriptedToolsMgr) -> Agent:
    """构造只接线工具执行与提醒的最小 agent。"""
    agent = object.__new__(Agent)
    agent.spec = AgentSpec(agent_type="main", description="")
    agent.uuid = uuid.uuid4()
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent.assembly = SimpleNamespace(
        reminder_mgr=SimpleNamespace(notify_tool_round=lambda names: None),
    )
    agent.deps = SimpleNamespace(tools_mgr=tools_mgr)
    return agent


def _tool_metadata(message: dict) -> dict:
    """解析工具消息首行的元数据 JSON。"""
    head, _, _body = str(message["content"]).partition("\n")
    return json.loads(head)


def _call(call_id: str, name: str) -> dict:
    """构造一条工具调用参数。"""
    return {"id": call_id, "name": name, "arguments": "{}"}


def _run_interrupted_tools(
    agent: Agent,
    ctx: RunContext,
    tools_mgr: _ScriptedToolsMgr,
) -> None:
    """执行工具轮并在第一个工具进行中取消整个轮次。"""

    async def scenario() -> None:
        task = asyncio.create_task(agent._on_execute_tools(ctx))
        await asyncio.wait_for(tools_mgr.entered.wait(), timeout=1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_interrupt_finalize_records_aborted_and_not_executed_tools() -> None:
    """在途调用补"被用户中止"，未派发调用补"未执行"，并追加中断标记。"""
    tools_mgr = _ScriptedToolsMgr(blocking={"slow"}, parallel=False)
    agent = _agent(tools_mgr)
    agent.history = [
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_slow", "name": "slow", "arguments": "{}"},
            {"id": "call_after", "name": "after", "arguments": "{}"},
        ]},
    ]
    ctx = RunContext(
        messages=agent.history,
        round_start_idx=1,
        user_message_index=0,
        response=LLMResponse(content="", tool_calls={
            0: _call("call_slow", "slow"),
            1: _call("call_after", "after"),
        }),
    )

    _run_interrupted_tools(agent, ctx, tools_mgr)
    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == [
        "user", "assistant", "tool", "tool", "user",
    ]
    aborted = _tool_metadata(agent.history[2])
    assert aborted["status"] == "cancelled"
    assert aborted["error_code"] == "aborted_by_user"
    assert "被用户中止" in agent.history[2]["content"]
    skipped = _tool_metadata(agent.history[3])
    assert skipped["error_code"] == "not_executed"
    assert "未执行即被用户中止" in agent.history[3]["content"]
    assert agent.history[4]["role"] == "user"
    assert "<turn_aborted>" in agent.history[4]["content"]


def test_interrupt_finalize_is_idempotent() -> None:
    """重复收尾不新增任何消息，也不重复补工具结果。"""
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    agent.history = [
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "name": "lookup", "arguments": "{}"},
        ]},
    ]
    ctx = RunContext(messages=agent.history, round_start_idx=1, user_message_index=0)

    agent._finalize_interrupted_turn(ctx)
    finalized = [dict(message) for message in agent.history]
    agent._finalize_interrupted_turn(ctx)

    assert agent.history == finalized
    assert [message["role"] for message in agent.history] == [
        "user", "assistant", "tool", "user",
    ]
    assert _tool_metadata(agent.history[2])["error_code"] == "not_executed"


def test_interrupt_finalize_skips_turn_without_user_message() -> None:
    """用户消息尚未入历史（等待输入阶段取消）时不追加任何标记。"""
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    agent.history = [{"role": "assistant", "content": "旧答案"}]

    agent._finalize_interrupted_turn(RunContext(messages=agent.history))

    assert agent.history == [{"role": "assistant", "content": "旧答案"}]


def test_interrupt_without_tool_call_adds_no_marker() -> None:
    """没动过工具时中断：即使已有可见正文也不追加只有重复语义的标记。"""
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    state = SessionState()
    agent.deps.session = SimpleNamespace(state=state, id="session")
    user_message = {"role": "user", "content": "当前问题"}
    user_record_id = state.append_user("当前问题", user_message)
    state.record_view(
        ViewPayload("assistant", {"content": "已经写了一半的回答"}),
        kind="assistant",
    )
    agent.history = state.context_messages()
    ctx = RunContext(
        messages=agent.history,
        user_message_index=0,
        user_record_id=user_record_id,
    )

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == ["user"]


def test_interrupt_with_completed_tools_adds_no_marker() -> None:
    """全部工具都正常返回时中断：没有部分执行风险，不追加标记。"""
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    agent.history = [
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_done", "name": "lookup", "arguments": "{}"},
        ]},
        {"role": "tool", "tool_call_id": "call_done", "content": str(ToolResult("ok"))},
    ]
    ctx = RunContext(
        messages=agent.history,
        user_message_index=0,
        response=LLMResponse(content="", tool_calls={0: _call("call_done", "lookup")}),
    )

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == ["user", "assistant", "tool"]


def test_interrupt_finalize_without_tool_call_adds_no_records() -> None:
    """纯正文轮次中断：模型消息与展示记录保持同一批对象，不新增任何记录。"""
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    state = SessionState()
    agent.deps.session = SimpleNamespace(state=state, id="session")
    user_message = {"role": "user", "content": "当前问题"}
    user_record_id = state.append_user("当前问题", user_message)
    state.record_view(
        ViewPayload("assistant", {"content": "已经写了一半的回答"}),
        kind="assistant",
    )
    agent.history = state.context_messages()
    before = list(state.records)
    ctx = RunContext(
        messages=agent.history,
        user_message_index=0,
        user_record_id=user_record_id,
    )

    agent._finalize_interrupted_turn(ctx)

    assert state.context_messages() == agent.history
    assert state.context_messages()[0] is agent.history[0]
    assert state.records == before
    assert [record.kind for record in state.visible_records()] == ["user", "assistant"]


def test_execute_tools_keeps_completed_results_when_cancelled() -> None:
    """取消不丢已完成的工具结果，只给未完成的调用补中止结果。"""
    tools_mgr = _ScriptedToolsMgr(blocking={"slow"}, parallel=True)
    agent = _agent(tools_mgr)
    agent.history = [{"role": "user", "content": "当前问题"}]
    ctx = RunContext(
        messages=agent.history,
        user_message_index=0,
        response=LLMResponse(content="", tool_calls={
            0: _call("call_fast", "fast"),
            1: _call("call_slow", "slow"),
        }),
    )

    _run_interrupted_tools(agent, ctx, tools_mgr)

    results = [message for message in agent.history if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["call_fast", "call_slow"]
    assert _tool_metadata(results[0])["status"] == "success"
    assert results[0]["content"].endswith("ok")
    assert _tool_metadata(results[1])["error_code"] == "aborted_by_user"

class _InterruptedLLM:
    """登记了一次被取消尝试的 provider 门面。"""

    def __init__(
        self,
        call: LLMCallContext,
        provider_type: type = DeepSeekProvider,
    ) -> None:
        """初始化门面。

        Args:
            call: 在流式调用内部被取消的尝试上下文。
            provider_type: 复用其载体组装逻辑的 provider 类。
        """
        self._call = call
        self.provider = object.__new__(provider_type)
        self.taken = 0

    def take_interrupted_call(self) -> LLMCallContext | None:
        """返回并清空被取消的尝试上下文。"""
        self.taken += 1
        call, self._call = self._call, None
        return call

    def interrupted_assistant_message(self, call: LLMCallContext) -> dict | None:
        """复用真实 Responses provider 的载体组装逻辑。"""
        return self.provider.interrupted_assistant_message(call)


def _stateful_agent() -> tuple[Agent, SessionState, str]:
    """构造带会话状态、框架消息与本轮用户记录的 agent。

    Returns:
        agent、会话状态与本轮用户记录 ID；history 为 [framework, user]。
    """
    agent = _agent(_ScriptedToolsMgr(blocking=set(), parallel=True))
    state = SessionState()
    agent.deps.session = SimpleNamespace(state=state, id="session")
    user_record_id = state.append_user("当前问题")
    state.append_context({"role": "developer", "content": "提醒"}, kind="developer")
    state.bind_model_message(
        {"role": "user", "content": "当前问题"},
        record_id=user_record_id,
        kind="user",
    )
    agent.history = state.context_messages()
    return agent, state, user_record_id


def _turn_context(
    agent: Agent,
    state: SessionState,
    user_record_id: str,
    **overrides: object,
) -> RunContext:
    """构造与本轮记录对齐的运行上下文。"""
    fields: dict[str, object] = {
        "round_start_idx": 0,
        "user_message_index": 1,
        "user_record_id": user_record_id,
        "llm_call_start_idx": len(agent.history),
        "llm_call_context_start": len(state.context_ids),
    }
    fields.update(overrides)
    return RunContext(messages=agent.history, **fields)  # type: ignore[arg-type]


def test_interrupt_without_complete_parts_keeps_user_input() -> None:
    """没有任何完整部件时：只丢弃未完成的模型输出，用户输入保留。"""
    agent, state, user_record_id = _stateful_agent()
    call = LLMCallContext(attempt=1, call_id="call_half")
    call.record_thinking_delta("半截思考")
    state.record_view(
        ViewPayload("assistant", {"thinking": "半截思考"}),
        kind="assistant",
        correlation_id="call_half",
        merge=True,
    )
    agent.assembly.llm = _InterruptedLLM(call)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == ["developer", "user"]
    assert state.context_messages() == agent.history
    assert [record.kind for record in state.records] == ["user", "developer", "assistant"]
    assert state.visible_records()[-1].view.data["thinking"] == "半截思考"
    assert all(
        record.model_message is None
        for record in state.records
        if record.kind == "assistant"
    )


def test_interrupt_keeps_complete_content_bound_to_stream_record() -> None:
    """完整正文部件收束成助手消息，并绑定到本轮流式展示记录。"""
    agent, state, user_record_id = _stateful_agent()
    call = LLMCallContext(attempt=1, call_id="call_text")
    carrier = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "完整回答"}],
    }
    call.open_part("content", "output:0")
    call.record_response_delta("完整回答", anchor="output:0")
    call.complete_part("output:0", carrier=carrier)
    state.record_view(
        ViewPayload("assistant", {"content": "完整回答"}),
        kind="assistant",
        correlation_id="call_text",
        merge=True,
    )
    agent.assembly.llm = _InterruptedLLM(call)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == ["developer", "user", "assistant"]
    assert agent.history[-1] == {"role": "assistant", "_response_output": [carrier]}
    assistant_records = [record for record in state.records if record.kind == "assistant"]
    assert len(assistant_records) == 1
    assert assistant_records[0].model_message is agent.history[-1]


def test_interrupt_keeps_complete_tool_call_and_marks_abort() -> None:
    """完整的工具调用收束成助手消息，并补未执行结果与中断标记。"""
    agent, state, user_record_id = _stateful_agent()
    call = LLMCallContext(attempt=1, call_id="call_tool")
    call.record_tool_fragment(0, call_id="call_1", name="lookup", arguments='{"q":"x"}')
    carrier = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"q":"x"}',
    }
    call.complete_part("call:0", carrier=carrier)
    agent.assembly.llm = _InterruptedLLM(call)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == [
        "developer", "user", "assistant", "tool", "user",
    ]
    assert agent.history[2] == {"role": "assistant", "_response_output": [carrier]}
    assert _tool_metadata(agent.history[3])["error_code"] == "not_executed"
    assert "<turn_aborted>" in agent.history[4]["content"]


def test_interrupt_keeps_complete_thinking_without_complete_tool_call() -> None:
    """思考完整但工具调用不完整时，完整推理载体照常回放，未完成的工具调用丢弃。"""
    agent, state, user_record_id = _stateful_agent()
    call = LLMCallContext(attempt=1, call_id="call_orphan")
    call.open_part("thinking", "output:0")
    call.record_thinking_delta("完整思考", anchor="output:0")
    carrier = {"type": "thinking", "thinking": "完整思考", "signature": "sig"}
    call.complete_part("output:0", carrier=carrier)
    # 工具调用已开始但没有结束标记，属于不完整部件，不构成推理载体的陪伴。
    call.record_tool_fragment(0, call_id="call_1", name="lookup", arguments='{"q":')
    state.record_view(
        ViewPayload("assistant", {"thinking": "完整思考"}),
        kind="assistant",
        correlation_id="call_orphan",
        merge=True,
    )
    agent.assembly.llm = _InterruptedLLM(call, AnthropicProvider)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == [
        "developer", "user", "assistant",
    ]
    assert agent.history[-1] == {"role": "assistant", "_anthropic_content": [carrier]}
    assert [record.kind for record in state.records] == ["user", "developer", "assistant"]
    assert state.visible_records()[-1].view.data["thinking"] == "完整思考"


def test_interrupt_keeps_orphan_reasoning_carrier() -> None:
    """只产出思考的中断轮保留完整推理载体、不回滚。"""
    agent, state, user_record_id = _stateful_agent()
    call = LLMCallContext(attempt=1, call_id="call_orphan_kept")
    call.open_part("thinking", "output:0")
    call.record_thinking_delta("完整思考", anchor="output:0")
    carrier = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "完整思考"}],
        "encrypted_content": "enc-0",
        "status": "completed",
    }
    call.complete_part("output:0", carrier=carrier)
    # 工具调用没有结束标记，不构成陪伴；完整推理载体不再要求工具调用陪伴。
    call.record_tool_fragment(0, call_id="call_1", name="lookup", arguments='{"q":')
    state.record_view(
        ViewPayload("assistant", {"thinking": "完整思考"}),
        kind="assistant",
        correlation_id="call_orphan_kept",
        merge=True,
    )
    agent.assembly.llm = _InterruptedLLM(call, DeepSeekProvider)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert [message["role"] for message in agent.history] == [
        "developer", "user", "assistant",
    ]
    assert agent.history[-1] == {"role": "assistant", "_response_output": [carrier]}
    assistant_records = [record for record in state.records if record.kind == "assistant"]
    assert len(assistant_records) == 1
    assert assistant_records[0].model_message is agent.history[-1]
    assert state.visible_records()[-1].view.data["thinking"] == "完整思考"
    assert not any(
        "<turn_aborted>" in str(message.get("content")) for message in agent.history
    )


def test_interrupt_rolls_back_only_current_call_in_later_round() -> None:
    """多轮次回合里中断：只回滚本次调用，已完成轮次保持可见。"""
    agent, state, user_record_id = _stateful_agent()
    assistant_round_one = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_done", "name": "lookup", "arguments": "{}"},
    ]}
    tool_round_one = {
        "role": "tool",
        "tool_call_id": "call_done",
        "content": str(ToolResult("ok")),
    }
    agent._append_message(agent.history, assistant_round_one, kind="assistant")
    agent._append_message(
        agent.history, tool_round_one, correlation_id="call_done", kind="tool",
    )
    call = LLMCallContext(attempt=1, call_id="call_second")
    call.record_thinking_delta("半截思考")
    agent.assembly.llm = _InterruptedLLM(call)
    ctx = _turn_context(agent, state, user_record_id)

    agent._finalize_interrupted_turn(ctx)

    assert agent.history == [
        state.context_messages()[0],
        state.context_messages()[1],
        state.context_messages()[2],
        state.context_messages()[3],
    ]
    assert [message["role"] for message in agent.history] == [
        "developer", "user", "assistant", "tool",
    ]

class _RecordingBus:
    """把事件投影写入会话状态的最小事件总线。"""

    def __init__(self, state: SessionState) -> None:
        """绑定接收事件的会话状态。"""
        self.state = state

    async def emit(self, event: object) -> None:
        """把事件投影写入会话状态。"""
        self.state.record_event(event)


class _StubPromptMgr:
    """最小提示词组装器：框架提醒段全部为空。"""

    def build_capability_instructions(self, mode: RunMode) -> str:
        """返回空能力指令。"""
        del mode
        return ""

    def build_manager_instructions(self) -> str:
        """返回空管理指令。"""
        return ""

    def build_mode_instructions(self, mode: RunMode) -> str:
        """返回空模式指令。"""
        del mode
        return ""


class _BlockingStream:
    """先产出预设事件、耗尽后阻塞到任务被取消的 Responses 流。"""

    def __init__(self, events: list[object], entered: asyncio.Event) -> None:
        """初始化流。

        Args:
            events: 依次产出的事件。
            entered: 事件耗尽时置位，供测试确定取消时机。
        """
        self._events = list(events)
        self._entered = entered

    def __aiter__(self) -> "_BlockingStream":
        """返回当前异步迭代器。"""
        return self

    async def __anext__(self) -> object:
        """返回下一个事件；耗尽后阻塞直到被取消。"""
        if self._events:
            return self._events.pop(0)
        self._entered.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration


class _BlockingResponses:
    """返回阻塞流并记录请求的 Responses 客户端替身。"""

    def __init__(self, events: list[object]) -> None:
        """初始化客户端替身。

        Args:
            events: 每次请求产出后即阻塞的事件序列。
        """
        self._events = events
        self.entered = asyncio.Event()
        self.requests: list[dict] = []

    async def create(self, **kwargs: object) -> _BlockingStream:
        """记录请求并返回阻塞流。"""
        self.requests.append(kwargs)
        return _BlockingStream(self._events, self.entered)


def _e2e_agent(tmp_path, events: list[object]) -> tuple[Agent, SessionMgr, DeepSeekProvider, _BlockingResponses]:
    """构造带真实 provider 与真实落盘的最小 agent。

    Args:
        tmp_path: 会话目录所在临时路径。
        events: provider 流在阻塞前产出的 Responses 事件。

    Returns:
        agent、会话管理器、provider 与流客户端替身。
    """
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    provider = DeepSeekProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="deepseek-test",
        event_bus=_RecordingBus(state),
        max_attempts=1,
    )
    responses = _BlockingResponses(events)
    provider._client = SimpleNamespace(responses=responses)
    agent = object.__new__(Agent)
    agent.spec = AgentSpec(agent_type="main", description="")
    agent.uuid = uuid.uuid4()
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent._handlers = {AgentState.LLM_CALL: agent._on_llm_call}
    agent._initial_context_added = True
    agent._manager_guidance_injected = True
    agent._last_capability_mode = agent.mode
    agent._last_injected_mode = agent.mode
    agent.assembly = SimpleNamespace(
        llm=provider,
        prompt_mgr=_StubPromptMgr(),
        reminder_mgr=SimpleNamespace(
            notify_tool_round=lambda names: None,
            queue_turn_start=lambda mode, is_subagent: None,
            pop_pending=lambda: [],
        ),
    )
    agent.deps = SimpleNamespace(
        tools_mgr=_ScriptedToolsMgr(blocking=set(), parallel=True),
        data_guard=None,
        event_bus=_RecordingBus(state),
        session_mgr=manager,
        session=SessionContext(id="session", state=state),
    )
    return agent, manager, provider, responses


def _prepare_turn(agent: Agent, state: SessionState) -> RunContext:
    """按真实顺序写入框架消息与本轮用户消息。"""
    agent._append_message(
        agent.history,
        {"role": "developer", "content": "提醒"},
        kind="developer",
    )
    user_record_id = state.append_user("当前问题")
    agent._append_message(
        agent.history,
        {"role": "user", "content": "当前问题"},
        record_id=user_record_id,
        kind="user",
    )
    return RunContext(
        messages=agent.history,
        round_start_idx=0,
        user_message_index=len(agent.history) - 1,
        user_record_id=user_record_id,
    )


def _cancel_during_llm_call(agent: Agent, ctx: RunContext, responses: _BlockingResponses) -> None:
    """在 provider 流阻塞后取消整个单轮状态机。"""

    async def scenario() -> None:
        task = asyncio.create_task(agent._run_single_turn(ctx, AgentState.LLM_CALL))
        await asyncio.wait_for(responses.entered.wait(), timeout=1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def _deepseek_item_done(
    output_index: int,
    *,
    item_id: str,
    item_type: str,
    content: list[dict] | None = None,
) -> object:
    """构造一个完整的 Responses 输出项事件。"""
    return SimpleNamespace(
        type="response.output_item.done",
        output_index=output_index,
        item=SimpleNamespace(
            model_dump=lambda exclude_none=True: {
                "id": item_id,
                "type": item_type,
                **({"content": content} if content else {}),
            },
            type=item_type,
            id=item_id,
        ),
    )


def test_interrupt_during_streaming_keeps_input_and_round_trips(tmp_path) -> None:
    """流式调用内部中断且无完整部件：只丢未完成输出，用户输入保留且日志可恢复。"""
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(type="message", id="msg_1"),
        ),
        SimpleNamespace(
            type="response.output_text.delta", output_index=0, delta="半截正文",
        ),
    ]
    agent, manager, provider, responses = _e2e_agent(tmp_path, events)
    state = agent.deps.session.state
    ctx = _prepare_turn(agent, state)

    _cancel_during_llm_call(agent, ctx, responses)
    asyncio.run(agent._commit_session("当前问题"))

    assert [message["role"] for message in agent.history] == ["developer", "user"]
    assert state.context_messages() == agent.history
    assert [record.kind for record in state.visible_records()] == ["user", "assistant"]
    assert state.visible_records()[-1].view.data["content"] == "半截正文"

    log_path = tmp_path / "sessions" / "session.jsonl"
    payload, _contents, _seq = fold_log(
        iter_batch_lines(log_path.read_text(encoding="utf-8"))
    )
    restored = SessionState.from_dict(payload)
    assert restored is not None
    assert restored.to_dict() == state.to_dict()
    assert restored.context_messages() == state.context_messages()
    assert [message["role"] for message in restored.context_messages()] == [
        "developer",
        "user",
    ]

    # 用户接着输入时，被中断的那条输入仍在上下文里，与新输入一起交给模型。
    messages = restored.context_messages()
    messages.append({"role": "user", "content": "继续"})
    _instructions, input_items = provider._convert_to_input(messages, None)
    assert [item.get("role") or item.get("type") for item in input_items] == [
        "developer",
        "user",
        "user",
    ]
    assert [item["content"] for item in input_items[-2:]] == ["当前问题", "继续"]


def test_interrupt_during_streaming_keeps_complete_message_item(tmp_path) -> None:
    """流式调用内部中断但正文项已结束：收束成助手消息并保留输入。"""
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(type="message", id="msg_1"),
        ),
        SimpleNamespace(
            type="response.output_text.delta", output_index=0, delta="完整回答",
        ),
        _deepseek_item_done(
            0,
            item_id="msg_1",
            item_type="message",
            content=[{"type": "output_text", "text": "完整回答"}],
        ),
    ]
    agent, _manager, _provider, responses = _e2e_agent(tmp_path, events)
    state = agent.deps.session.state
    ctx = _prepare_turn(agent, state)

    _cancel_during_llm_call(agent, ctx, responses)

    assert [message["role"] for message in agent.history] == [
        "developer",
        "user",
        "assistant",
    ]
    assert agent.history[-1]["_response_output"][0]["content"] == [
        {"type": "output_text", "text": "完整回答"},
    ]
    assistant_records = [
        record for record in state.records if record.kind == "assistant"
    ]
    assert len(assistant_records) == 1
    assert assistant_records[0].model_message is agent.history[-1]

def test_resume_after_interrupt_sends_clean_history(tmp_path) -> None:
    """恢复记录后继续：下一次请求只带完整消息与最新用户输入。"""
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(type="message", id="msg_1"),
        ),
        SimpleNamespace(
            type="response.output_text.delta", output_index=0, delta="完整回答",
        ),
        _deepseek_item_done(
            0,
            item_id="msg_1",
            item_type="message",
            content=[{"type": "output_text", "text": "完整回答"}],
        ),
    ]
    agent, _manager, provider, responses = _e2e_agent(tmp_path, events)
    state = agent.deps.session.state
    ctx = _prepare_turn(agent, state)

    _cancel_during_llm_call(agent, ctx, responses)
    asyncio.run(agent._commit_session("当前问题"))

    # 模拟进程重启：只从日志恢复会话状态。
    log_path = tmp_path / "sessions" / "session.jsonl"
    payload, _contents, _seq = fold_log(
        iter_batch_lines(log_path.read_text(encoding="utf-8"))
    )
    restored = SessionState.from_dict(payload)
    assert restored is not None
    messages = restored.context_messages()
    assert [message["role"] for message in messages] == [
        "developer",
        "user",
        "assistant",
    ]
    assert messages[2]["_response_output"][0]["content"] == [
        {"type": "output_text", "text": "完整回答"},
    ]

    # 用户接着输入时，上下文里没有半截正文、也没有重复的用户消息。
    messages.append({"role": "user", "content": "继续"})
    _instructions, input_items = provider._convert_to_input(messages, None)
    assert [item.get("role") or item.get("type") for item in input_items] == [
        "developer", "user", "message", "user",
    ]
    assert input_items[-1]["content"] == "继续"
