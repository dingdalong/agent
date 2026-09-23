from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src.app.app import AgentApp
from src.agent import SessionContext
from src.events.types import (
    InteractionCompleted,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.interfaces.agent_view_store import AgentViewStore, ContextUsage, TokenUsage
from src.interfaces.output_router import OutputRouter
from src.mgr.session_mgr import SessionMgr
from src.mgr.session_mgr import ResumeResult
from src.common.session_log import LOG_VERSION
from src.common.session_state import SessionState, ViewPayload
from src.common.tool_display import ToolDisplay
from src.mode import RunMode


class _UI:
    def __init__(self) -> None:
        self.events = []

    async def on_event(self, event) -> None:
        self.events.append(event)


def test_user_record_keeps_raw_input_and_injected_model_message_together() -> None:
    state = SessionState()
    record_id = state.append_user("原始输入")
    injected = {"role": "user", "content": "<reminder>内部提醒</reminder>\n\n原始输入"}

    state.bind_model_message(injected, record_id=record_id, kind="user")

    assert state.context_messages() == [injected]
    assert state.input_history() == ["原始输入"]
    assert state.visible_records()[0].view.data == {"text": "原始输入"}


def test_unrelated_context_messages_append_as_distinct_records() -> None:
    state = SessionState()
    first = {"role": "user", "content": "续写指令"}
    second = {"role": "user", "content": "任务提醒"}

    state.bind_model_message(first)
    state.bind_model_message(second)

    assert state.context_messages() == [first, second]


def test_router_merges_assistant_stream_by_call_id_then_binds_model_message() -> None:
    async def scenario() -> None:
        state = SessionState()
        store = AgentViewStore()
        store.register_foreground("main-id", "main")
        ui = _UI()
        router = OutputRouter(ui, store, session_state=state)

        await router.dispatch(ThinkingDelta(
            timestamp=1.0, source="model", content="思考", call_id="call-1",
            caller_agent_type="main", caller_uuid="main-id",
        ))
        await router.dispatch(ResponseDelta(
            timestamp=2.0, source="model", content="回答", call_id="call-1",
            caller_agent_type="main", caller_uuid="main-id",
        ))
        message = {"role": "assistant", "content": "回答", "reasoning_content": "思考"}
        state.bind_model_message(message, correlation_id="call-1", kind="assistant")

        assert len(state.records) == 1
        assert state.records[0].view.data == {"thinking": "思考", "content": "回答"}
        assert state.context_messages() == [message]

    asyncio.run(scenario())


def test_router_persists_llm_attempt_usage_for_foreground_and_background() -> None:
    async def scenario() -> None:
        state = SessionState()
        store = AgentViewStore()
        store.register_foreground("main-id", "main")
        router = OutputRouter(_UI(), store, session_state=state)

        for caller in ("main-id", "worker-id"):
            await router.dispatch(LLMCallStarted(
                timestamp=1.0, source="provider/model", model="model", call_id=f"call-{caller}",
                attempt=1, reasoning_effort="max", phase="plan",
                caller_agent_type="main" if caller == "main-id" else "worker", caller_uuid=caller,
            ))
            await router.dispatch(LLMCallCompleted(
                timestamp=2.0, source="provider/model", model="model", call_id=f"call-{caller}",
                attempt=1, outcome="completed", reasoning_effort="max", phase="plan",
                input_tokens=100, output_tokens=20, reasoning_output_tokens=10,
                cache_read_input_tokens=80,
                caller_agent_type="main" if caller == "main-id" else "worker", caller_uuid=caller,
            ))

        assert len(state.llm_calls) == 2
        assert {(item.caller_uuid, item.input_tokens) for item in state.llm_calls} == {
            ("main-id", 100), ("worker-id", 100),
        }
        restored = SessionState.from_dict(state.to_dict())
        assert restored is not None
        assert restored.llm_calls == state.llm_calls

    asyncio.run(scenario())


def test_failed_attempt_and_interaction_answer_are_persisted() -> None:
    state = SessionState()
    state.record_llm_event(LLMCallStarted(
        timestamp=1.0, source="provider/model", model="model", call_id="failed", attempt=2,
        phase="execute", caller_agent_type="main", caller_uuid="main-id",
    ))
    state.record_llm_event(LLMCallFailed(
        timestamp=2.0, source="provider/model", call_id="failed", attempts=2,
        error_kind="timeout", caller_agent_type="main", caller_uuid="main-id",
    ))
    state.record_event(InteractionCompleted(
        timestamp=3.0, source="ask_user", request_type="form_menu",
        summary="作答已提交", cancelled=False,
        request={"type": "form_menu", "questions": [{"header": "范围", "question": "选择范围"}]},
        answer='{"scope":"minimal"}', caller_agent_type="main", caller_uuid="main-id",
    ))

    assert state.llm_calls[0].outcome == "timeout"
    interaction = state.visible_records()[-1].view.data
    assert interaction["request"]["questions"][0]["header"] == "范围"
    assert interaction["answer"] == '{"scope":"minimal"}'


def test_tool_events_and_model_result_merge_by_tool_call_id() -> None:
    state = SessionState()
    state.record_event(ToolCallStarted(
        timestamp=1.0, source="tools", tool_name="exec_command", tool_call_id="tool-1",
        detail="a.py",
    ))
    state.record_event(ToolCallCompleted(
        timestamp=2.0, source="tools", tool_name="exec_command", tool_call_id="tool-1",
        status="success", result_preview="ok", duration_seconds=0.2,
    ))
    message = {"role": "tool", "tool_call_id": "tool-1", "content": "ok"}
    state.bind_model_message(message, correlation_id="tool-1", kind="tool")

    assert len(state.records) == 1
    assert set(state.records[0].view.data) == {"started", "completed"}
    assert state.context_messages() == [message]


def test_tool_display_recipe_keeps_result_text_only_in_model_message() -> None:
    state = SessionState()
    state.record_event(ToolCallCompleted(
        timestamp=2.0, source="tools", tool_name="read_file", tool_call_id="tool-1",
        status="success", result_preview="结果正文", duration_seconds=0.2,
        display=ToolDisplay(
            title="读取文件",
            content="结果正文",
            render={"kind": "result", "mode": "truncate"},
        ),
    ))
    state.bind_model_message(
        {"role": "tool", "tool_call_id": "tool-1", "content": "结果正文"},
        correlation_id="tool-1",
        kind="tool",
    )

    record = state.records[0]
    assert record.view is not None
    display = record.view.data["completed"]["display"]
    assert display["content"] == ""
    assert display["render"] == {"kind": "result", "mode": "truncate"}
    assert record.model_message == {"role": "tool", "tool_call_id": "tool-1", "content": "结果正文"}
    assert state.tool_result_text("tool-1") == "结果正文"


def test_router_persists_and_restores_subagent_snapshot() -> None:
    async def scenario() -> None:
        state = SessionState()
        store = AgentViewStore()
        store.register_foreground("main-id", "main")
        router = OutputRouter(_UI(), store, session_state=state)

        await router.dispatch(SubagentLifecycle(
            timestamp=1.0,
            source="subagent_mgr",
            agent_uuid="worker-id",
            agent_type="worker",
            phase="start",
            task="检查代码",
        ))
        await router.dispatch(ResponseDelta(
            timestamp=2.0,
            source="model",
            content="子 agent 回答",
            caller_agent_type="worker",
            caller_uuid="worker-id",
        ))
        await router.dispatch(SubagentLifecycle(
            timestamp=3.0,
            source="subagent_mgr",
            agent_uuid="worker-id",
            agent_type="worker",
            phase="end",
            task="检查代码",
            messages=[
                {"role": "user", "content": "检查代码"},
                {"role": "assistant", "content": "子 agent 回答"},
            ],
        ))

        views = state.subagent_views()
        assert len(views) == 1
        assert views[0]["uuid"] == "worker-id"
        assert views[0]["messages"][-1]["content"] == "子 agent 回答"
        assert state.visible_records() == []

        restored = AgentViewStore()
        restored.restore_subagents(views)
        snapshot = restored.subagent_snapshots()[0]
        assert snapshot.uuid == "worker-id"
        assert snapshot.agent_type == "worker"
        assert not snapshot.running
        assert restored.transcript_messages("worker-id")[-1]["content"] == "子 agent 回答"
        # 完整消息快照已覆盖增量转录，转录不再保留同一段正文的第二份副本。
        assert restored.transcript_segments("worker-id") == []

    asyncio.run(scenario())


def test_router_exports_subagent_snapshot_only_at_end() -> None:
    async def scenario() -> None:
        state = SessionState()
        store = AgentViewStore()
        store.register_foreground("main-id", "main")
        router = OutputRouter(_UI(), store, session_state=state)
        exports = 0
        original_export = store.export_subagent

        def count_export(uuid: str):
            nonlocal exports
            exports += 1
            return original_export(uuid)

        store.export_subagent = count_export  # type: ignore[method-assign]
        await router.dispatch(SubagentLifecycle(
            timestamp=1.0,
            source="subagent_mgr",
            agent_uuid="worker-id",
            agent_type="worker",
            phase="start",
        ))
        for _ in range(1_000):
            await router.dispatch(ResponseDelta(
                timestamp=2.0,
                source="model",
                content="x",
                caller_agent_type="worker",
                caller_uuid="worker-id",
            ))

        assert exports == 0
        assert state.subagent_views() == []
        signature = store.transcript_signature("worker-id")
        assert signature[0] == 1_000
        assert signature[1] == 1_000

        await router.dispatch(SubagentLifecycle(
            timestamp=3.0,
            source="subagent_mgr",
            agent_uuid="worker-id",
            agent_type="worker",
            phase="end",
            messages=[{"role": "assistant", "content": "x" * 1_000}],
        ))

        assert exports == 1
        assert len(state.subagent_views()) == 1
        assert state.subagent_views()[0]["messages"] == [
            {"role": "assistant", "content": "x" * 1_000}
        ]
        assert state.subagent_views()[0]["transcript"] == []

    asyncio.run(scenario())


def test_compact_replaces_context_without_deleting_visible_chat() -> None:
    state = SessionState()
    user_id = state.append_user("问题", {"role": "user", "content": "问题"})
    state.record_event(ResponseDelta(
        timestamp=2.0, source="model", content="完整回答", call_id="call-1",
    ))
    state.bind_model_message(
        {"role": "assistant", "content": "完整回答"},
        correlation_id="call-1",
        kind="assistant",
    )

    state.replace_context([{"role": "user", "content": "压缩摘要"}])

    assert state.context_messages() == [{"role": "user", "content": "压缩摘要"}]
    assert [record.id for record in state.visible_records()][:1] == [user_id]
    assert [record.view.kind for record in state.visible_records()] == ["user", "assistant"]


def test_same_length_compact_does_not_rebind_visible_records_positionally() -> None:
    state = SessionState()
    user_id = state.append_user("原问题", {"role": "user", "content": "原问题"})
    state.record_event(ResponseDelta(
        timestamp=2.0, source="model", content="原回答", call_id="call-1",
    ))
    state.bind_model_message(
        {"role": "assistant", "content": "原回答"},
        correlation_id="call-1",
        kind="assistant",
    )

    state.replace_context([
        {"role": "user", "content": "压缩摘要"},
        {"role": "assistant", "content": "继续所需状态"},
    ])

    assert state.context_messages()[0]["content"] == "压缩摘要"
    original = next(record for record in state.records if record.id == user_id)
    assert original.model_message is None
    assert original.view.data == {"text": "原问题"}


def test_stream_view_chunks_materialize_only_at_read_boundaries() -> None:
    state = SessionState()
    for _ in range(50_000):
        state.record_event(ResponseDelta(
            timestamp=2.0,
            source="model",
            content="x",
            call_id="call-stream",
        ))

    record = state.records[0]
    buffer = state._view_streams[(record.id, "content")]
    assert record.view is not None
    assert record.view.data["content"] == ""
    assert len(buffer.chunks) == 50_000
    assert buffer.length == 50_000

    state.bind_model_message(
        {"role": "assistant", "content": "x" * 50_000},
        correlation_id="call-stream",
        kind="assistant",
    )

    assert record.view.data["content"] == "x" * 50_000
    assert not state._view_streams

    serialized = SessionState()
    serialized.record_event(ThinkingDelta(
        timestamp=3.0,
        source="model",
        content="chunked thought",
        call_id="call-serialize",
    ))
    payload = serialized.to_dict()
    assert payload["records"][0]["view"]["data"]["thinking"] == "chunked thought"
    assert not serialized._view_streams


def test_truncate_context_releases_only_removed_model_payloads() -> None:
    state = SessionState()
    first = state.append_context({"role": "user", "content": "first"})
    second = state.append_context({"role": "assistant", "content": "second"})

    state.truncate_context(1)

    by_id = {record.id: record for record in state.records}
    assert state.context_ids == [first]
    assert by_id[first].model_message is not None
    assert by_id[second].model_message is None


def test_session_manager_only_lists_valid_sessions_and_writes_current_files(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    valid = SessionState()
    valid.append_user("hello", {"role": "user", "content": "hello"})
    manager.save_metadata("valid", is_new=True, topic="hello")
    manager.append_state("valid", valid)
    assert [item["session_id"] for item in manager.list_resumable("current")] == ["valid"]
    assert manager.load_state("valid").context_messages() == valid.context_messages()
    sessions_dir = tmp_path / "sessions"
    assert (sessions_dir / "valid.jsonl").exists()


class _ResumeBus:
    """resume 门控用的事件总线桩：只记录 join 次数。"""

    def __init__(self) -> None:
        self.joins = 0

    @contextmanager
    def reject_ui_requests(self):
        yield

    async def join(self) -> None:
        self.joins += 1


class _ResumeUI:
    """resume 门控用的 UI 桩：记录切换后的状态与 provider 回调。"""

    def __init__(self) -> None:
        self.state = None

    @asynccontextmanager
    async def reset_session_interactions(self):
        yield

    async def replace_session_state(self, state) -> None:
        self.state = state

    def set_input_history_provider(self, provider) -> None:
        self.input_provider = provider

    def set_model_info_provider(self, provider) -> None:
        self.model_provider = provider


class _RecordingSessions:
    """resume 保存源会话时用的会话管理器桩。"""

    def __init__(self) -> None:
        self.saved = []

    def append_batch(self, session_id, payload, resync=None) -> None:
        self.saved.append((session_id, payload))


class _ResumeRouter:
    """resume 切换会话状态时用的输出路由桩。"""

    def bind_session_state(self, state) -> None:
        self.state = state


def _record_call(
    state: SessionState,
    call_id: str,
    *,
    caller_type: str | None,
    caller_uuid: str | None,
    started_at: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
    outcome: str = "completed",
) -> None:
    """按 provider 事件路径写入一次 attempt 的核账记录。

    ``outcome="running"`` 只写开始事件，模拟未完成的调用。
    """
    state.record_llm_event(LLMCallStarted(
        timestamp=started_at, source="provider/model", model="model", call_id=call_id,
        attempt=1, caller_agent_type=caller_type, caller_uuid=caller_uuid,
    ))
    if outcome == "running":
        return
    state.record_llm_event(LLMCallCompleted(
        timestamp=started_at + 0.5, source="provider/model", model="model", call_id=call_id,
        attempt=1, outcome=outcome, caller_agent_type=caller_type, caller_uuid=caller_uuid,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
    ))


def test_app_resume_switches_state_inside_gates_and_resets_transient_store() -> None:

    source = SessionState()
    source.append_user("旧会话", {"role": "user", "content": "旧会话"})
    target = SessionState()
    target.append_user("恢复", {"role": "user", "content": "恢复"})
    target.record_subagent_snapshot({
        "uuid": "worker-target",
        "agent_type": "worker",
        "running": False,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0},
        "context": {"used_tokens": 10, "limit_tokens": 100},
        "elapsed_seconds": 1.5,
        "activity": "",
        "task": "目标会话任务",
        "transcript": [{"kind": "response", "text": "目标结果"}],
        "messages": [{"role": "assistant", "content": "目标结果"}],
    })
    bus = _ResumeBus()
    ui = _ResumeUI()
    sessions = _RecordingSessions()
    router = _ResumeRouter()
    deps = SimpleNamespace(
        event_bus=bus,
        ui=ui,
        session_mgr=sessions,
        session=SessionContext(id="source", state=source, context=["old"]),
        role_mgr=None,
    )
    app = AgentApp(deps, AgentViewStore(), router)  # type: ignore[arg-type]
    app._install_plan_mode_controller = lambda: None  # type: ignore[method-assign]
    fake_agent = SimpleNamespace(
        uuid="new-main",
        agent_type="main",
        llm=SimpleNamespace(model="model", reasoning_effort="medium", context_limit=200_000),
        reasoning_effort=None,
        mode=RunMode.PLAN,
        task_mgr=None,
        get_input_history=target.input_history,
    )
    result = ResumeResult(
        session_id="target",
        state=target,
        metadata={"topic": "主题", "mode": "plan"},
    )

    async def scenario() -> None:
        with patch("src.app.app.Agent.from_manifest", return_value=fake_agent):
            agent, summary = await app.resume_session(result)
        assert agent is fake_agent
        assert "已恢复会话 target" in summary

    asyncio.run(scenario())

    assert [item[0] for item in sessions.saved] == ["source"]
    assert sessions.saved[0][1]["records"][0]["model_message"] == {
        "role": "user",
        "content": "旧会话",
    }
    assert deps.session.id == "target"
    assert deps.session.state is target
    assert router.state is target
    assert ui.state is target
    assert app.agent_view_store.foreground_uuid == "new-main"
    assert [s.uuid for s in app.agent_view_store.subagent_snapshots()] == ["worker-target"]
    assert deps.session.context != ["old"]
    assert bus.joins == 2


def test_app_resume_restores_session_metrics_and_persists_source_elapsed() -> None:
    """恢复会话回填会话累计 token / 上下文 / 耗时，源会话耗时在切换前落盘。"""
    source = SessionState()
    source.append_user("旧会话", {"role": "user", "content": "旧会话"})
    target = SessionState()
    target.append_user("恢复", {"role": "user", "content": "恢复"})
    _record_call(
        target, "call-main-1", caller_type="main", caller_uuid="old-main",
        started_at=1.0, input_tokens=100, output_tokens=20,
        cache_read_input_tokens=30, reasoning_output_tokens=4,
    )
    _record_call(
        target, "call-worker", caller_type="worker", caller_uuid="worker-1",
        started_at=2.0, input_tokens=40, output_tokens=5, cache_read_input_tokens=8,
    )
    _record_call(
        target, "call-internal", caller_type=None, caller_uuid=None,
        started_at=3.0, input_tokens=12, output_tokens=3,
    )
    _record_call(
        target, "call-main-2", caller_type="main", caller_uuid="old-main",
        started_at=4.0, input_tokens=250, output_tokens=30, cache_read_input_tokens=200,
    )
    target.set_session_elapsed(120.0)

    bus = _ResumeBus()
    ui = _ResumeUI()
    sessions = _RecordingSessions()
    router = _ResumeRouter()
    store = AgentViewStore(clock=lambda: 1000.0)
    deps = SimpleNamespace(
        event_bus=bus,
        ui=ui,
        session_mgr=sessions,
        session=SessionContext(id="source", state=source, context=["old"]),
        role_mgr=None,
    )
    app = AgentApp(deps, store, router)  # type: ignore[arg-type]
    app._install_plan_mode_controller = lambda: None  # type: ignore[method-assign]
    store.add_session_elapsed(9.0)  # 源会话已累计的耗时
    fake_agent = SimpleNamespace(
        uuid="new-main",
        agent_type="main",
        llm=SimpleNamespace(model="model", reasoning_effort="medium", context_limit=200_000),
        reasoning_effort=None,
        mode=RunMode.EXECUTE,
        task_mgr=None,
        get_input_history=target.input_history,
    )
    result = ResumeResult(session_id="target", state=target, metadata={"topic": "主题"})

    async def scenario() -> None:
        with patch("src.app.app.Agent.from_manifest", return_value=fake_agent):
            await app.resume_session(result)

    asyncio.run(scenario())

    # 源会话的耗时在切换前并入载荷，保证 A→B→A 往复恢复不丢。
    assert sessions.saved[0][1]["metrics"] == {"elapsed_seconds": 9.0}
    snapshot = store.session_snapshot()
    assert snapshot.usage == TokenUsage(402, 58, 238, 4)
    assert snapshot.foreground_context == ContextUsage(250, 200_000)
    assert store.session_elapsed == 120.0
    main = store.agent_snapshot("new-main")
    assert main is not None
    assert main.usage == TokenUsage(350, 50, 230, 4)
    assert main.context == ContextUsage(250, 200_000)
    assert main.elapsed_seconds == 120.0


def test_live_store_elapsed_reaches_log_and_restores_into_store(tmp_path) -> None:
    """闭环：UI 结算的耗时经强制提交写进日志，恢复时折叠回状态并回填读模型。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_user("会话", {"role": "user", "content": "会话"})
    store = AgentViewStore()
    store.register_foreground("main-id", "main")
    deps = SimpleNamespace(
        session=SessionContext(id="live", state=state),
        session_mgr=manager,
        event_bus=None,
        ui=None,
        data_guard=None,
    )
    app = AgentApp(deps, store, _ResumeRouter())  # type: ignore[arg-type]

    async def scenario() -> None:
        store.add_session_elapsed(75.5)  # 回合结算（正常结束或被中断）后的累计值
        assert await app._append_session_state(manager, "live", state, force=True)

    asyncio.run(scenario())

    log_text = (tmp_path / "sessions" / "live.jsonl").read_text(encoding="utf-8")
    assert '"metrics"' in log_text

    restored = manager.load_state("live")
    assert restored is not None
    assert restored.session_elapsed == 75.5

    resumed = AgentViewStore()
    resumed.register_foreground("new-main", "main")
    resumed.restore_session_metrics(restored, context_limit=128_000)
    assert resumed.session_elapsed == 75.5


def test_taken_commit_is_stable_against_later_streaming() -> None:
    """已取出的增量是稳定快照：后续流式增量不能改写待落盘内容。"""
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "第一批"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )

    payload = state.take_commit()
    assert payload is not None
    state.record_view(
        ViewPayload("assistant", {"content": "追加"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )

    assert payload["records"][0]["view"]["data"]["content"] == "第一批"
    assert state.to_dict()["records"][0]["view"]["data"]["content"] == "第一批追加"


def test_prepared_commit_ack_preserves_streaming_arriving_during_write() -> None:
    """写入期间到达的正文不会随确认丢掉：它继续留在待写增量里。"""
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "第一批"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    commit = state.prepare_commit()
    assert commit is not None
    state.record_view(
        ViewPayload("assistant", {"content": "追加"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    state.ack_commit(commit)

    assert state.checkpoint_due(force=True)
    next_commit = state.prepare_commit()
    assert next_commit is not None
    assert "records" not in next_commit.payload
    delta = next_commit.payload["record_deltas"][0]
    assert delta["view"]["append"]["content"] == "追加"
    assert delta["stream_boundaries"]["content"]["after"] == next_commit.stream_state[
        (state.records[0].id, "content")
    ]


def test_checkpoint_interval_follows_pending_stream_size() -> None:
    """检查点按未确认正文的长度选档：正文越长越稀疏，无流式内容时最短。"""
    state = SessionState()
    started = state._last_checkpoint_at
    state.record_view(
        ViewPayload("assistant", {"content": "x" * 400}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    assert state.checkpoint_due(now=started + 0.1) is False
    assert state.checkpoint_due(now=started + 0.3) is True
    assert state.checkpoint_due(force=True) is True

    state.record_view(
        ViewPayload("assistant", {"content": "y" * 70_000}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    assert state.checkpoint_due(now=started + 0.6) is False
    assert state.checkpoint_due(now=started + 1.1) is True
    assert state._stream_length() == 70_400

    assert SessionState().checkpoint_due(force=True) is False


def test_equal_context_messages_do_not_mark_records_dirty() -> None:
    """provider 每轮回写归一化结果：内容相同时不产生新的待提交增量。"""
    state = SessionState()
    state.append_context({"role": "assistant", "content": "答案"})
    assert state.take_commit() is not None

    replacement = {"role": "assistant", "content": "答案"}
    state.set_context_message(0, replacement)
    assert state.records[0].model_message is replacement
    assert state.take_commit() is None

    state.replace_context(
        [{"role": "assistant", "content": "答案"}], preserve_positions=True,
    )
    assert state.take_commit() is None

    state.replace_context(
        [{"role": "assistant", "content": "改过的答案"}], preserve_positions=True,
    )
    changed = state.take_commit()
    assert changed is not None
    assert len(changed["records"]) == 1


def test_context_delta_only_commits_the_growing_tail() -> None:
    """上下文只提交增量：公共前缀不重复落盘，截断用更小的 keep 表达。"""
    state = SessionState()
    first = state.append_context({"role": "user", "content": "一"})
    second = state.append_context({"role": "assistant", "content": "二"})

    payload = state.take_commit()
    assert payload is not None
    assert payload["context"] == {"keep": 0, "append": [first, second]}

    third = state.append_context({"role": "user", "content": "三"})
    payload = state.take_commit()
    assert payload is not None
    assert payload["context"] == {"keep": 2, "append": [third]}

    state.truncate_context(1)
    payload = state.take_commit()
    assert payload is not None
    assert payload["context"] == {"keep": 1, "append": []}


def test_session_elapsed_commit_is_change_driven_and_round_trips() -> None:
    """会话耗时只在变化时产生提交，确认后清脏；非法与旧载荷按 0 兼容。"""
    state = SessionState()
    assert state.take_commit() is None

    state.set_session_elapsed(-1.0)
    assert state.session_elapsed == 0.0
    assert state.take_commit() is None

    state.set_session_elapsed(12.5)
    assert state.take_commit() == {"metrics": {"elapsed_seconds": 12.5}}

    state.set_session_elapsed(12.5)
    assert state.take_commit() is None

    state.set_session_elapsed(40.0)
    assert state.checkpoint_due(now=state._last_checkpoint_at + 10.0)
    assert state.take_commit() == {"metrics": {"elapsed_seconds": 40.0}}

    restored = SessionState.from_dict(state.to_dict())
    assert restored is not None
    assert restored.session_elapsed == 40.0
    assert restored.to_dict()["metrics"] == {"elapsed_seconds": 40.0}

    legacy = SessionState.from_dict({
        "version": LOG_VERSION,
        "records": [],
        "context_ids": [],
        "llm_calls": [],
    })
    assert legacy is not None
    assert legacy.session_elapsed == 0.0
    assert SessionState.from_dict({
        "version": LOG_VERSION,
        "records": [],
        "context_ids": [],
        "llm_calls": [],
        "metrics": {"elapsed_seconds": -3.0},
    }).session_elapsed == 0.0


def test_view_reuses_the_model_message_text_object() -> None:
    """同一段正文同时服务模型与前台展示时，内存里只保留一份对象。"""
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "答案"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    state.record_view(
        ViewPayload("assistant", {"thinking": "想一想"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    message = {
        "role": "assistant",
        "_response_output": [
            {"type": "message", "content": [{"text": "答案"}]},
            {"type": "reasoning", "summary": [{"text": "想一想"}]},
        ],
    }
    state.bind_model_message(message, correlation_id="call-1", kind="assistant")

    view = state.records[0].view
    assert view is not None
    assert view.data["content"] is message["_response_output"][0]["content"][0]["text"]
    assert view.data["thinking"] is message["_response_output"][1]["summary"][0]["text"]


def test_user_view_reuses_the_model_message_text_object() -> None:
    state = SessionState()
    record_id = state.append_user("用户输入")
    message = {"role": "user", "content": "用户" + "输入"}
    state.bind_model_message(message, record_id=record_id, kind="user")

    record = state.records[0]
    assert record.view is not None
    assert record.view.data["text"] is message["content"]
    assert record.raw_input is message["content"]


def test_tool_call_arguments_reads_the_single_stored_copy() -> None:
    """工具参数只从 assistant 消息里取回，展示正文按同一份数据重建。"""
    state = SessionState()
    state.append_context({
        "role": "assistant",
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "ls"})},
        }],
    })
    state.append_context({
        "role": "assistant",
        "_response_output": [{
            "type": "function_call",
            "call_id": "call-2",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "pwd"}),
        }],
    })

    assert state.tool_call_arguments("call-1") == {"cmd": "ls"}
    assert state.tool_call_arguments("call-2") == {"cmd": "pwd"}
    assert state.tool_call_arguments("call-3") is None


def test_tool_result_projection_is_stripped_but_rebuildable() -> None:
    """工具结果的展示正文与预览都是投影，落盘只保留展示配方。"""
    state = SessionState()
    state.record_event(ToolCallCompleted(
        timestamp=2.0, source="tools", tool_name="read_file", tool_call_id="tool-1",
        status="success", result_preview="结果正文", duration_seconds=0.2,
        display=ToolDisplay(
            title="读取文件",
            content="结果正文",
            render={"kind": "result", "mode": "truncate"},
        ),
    ))

    completed = state.records[0].view.data["completed"]
    assert completed["display"]["content"] == ""
    assert completed["result_preview"] == ""
    assert state.tool_result_text("tool-1") is None


def test_external_read_preview_keeps_its_privacy_summary() -> None:
    """没有展示配方的工具（外部读取）保留自己的摘要，不参与重建。"""
    state = SessionState()
    state.record_event(ToolCallCompleted(
        timestamp=2.0, source="tools", tool_name="web_fetch", tool_call_id="tool-1",
        status="success", result_preview="status=success, length=12", duration_seconds=0.2,
        display=ToolDisplay(title="获取网页", content="status=success, length=12"),
    ))

    completed = state.records[0].view.data["completed"]
    assert completed["display"]["content"] == "status=success, length=12"
    assert completed["result_preview"] == "status=success, length=12"


def test_agent_stores_provider_normalized_text() -> None:
    """写入会话状态前先按 provider 规则规范化正文，避免先写原样再改写。"""
    from src.agent.agent import Agent

    state = SessionState()
    agent = object.__new__(Agent)
    agent.history = []
    agent.deps = SimpleNamespace(session=SessionContext(id="s", state=state))
    agent.assembly = SimpleNamespace(
        llm=SimpleNamespace(normalize_stored_text=lambda text: text.strip()),
    )

    agent._append_message(
        agent.history,
        {"role": "tool", "tool_call_id": "call-1", "content": "结果正文\n"},
        correlation_id="call-1",
        kind="tool",
    )

    assert agent.history[0]["content"] == "结果正文"
    assert state.context_messages()[0]["content"] == "结果正文"
