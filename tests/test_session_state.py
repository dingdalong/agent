from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src.app.app import AgentApp
from src.events.types import (
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.output_router import OutputRouter
from src.mgr.session_mgr import SessionMgr
from src.mgr.session_mgr import ResumeResult
from src.mgr.session_state import SessionState


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


def test_tool_events_and_model_result_merge_by_tool_call_id() -> None:
    state = SessionState()
    state.record_event(ToolCallStarted(
        timestamp=1.0, source="tools", tool_name="read_file", tool_call_id="tool-1",
        detail="a.py",
    ))
    state.record_event(ToolCallCompleted(
        timestamp=2.0, source="tools", tool_name="read_file", tool_call_id="tool-1",
        status="success", result_preview="ok", duration_seconds=0.2,
    ))
    message = {"role": "tool", "tool_call_id": "tool-1", "content": "ok"}
    state.bind_model_message(message, correlation_id="tool-1", kind="tool")

    assert len(state.records) == 1
    assert set(state.records[0].view.data) == {"started", "completed"}
    assert state.context_messages() == [message]


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
        assert restored.transcript_segments("worker-id") == [("response", "子 agent 回答")]

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
        assert state.subagent_views()[0]["transcript"] == [
            {"kind": "response", "text": "x" * 1_000}
        ]

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


def test_session_manager_only_lists_valid_state_sessions_and_writes_no_legacy_files(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    valid = SessionState()
    valid.append_user("hello", {"role": "user", "content": "hello"})
    manager.save_metadata("valid", is_new=True, topic="hello")
    manager.save_state("valid", valid)
    manager.save_metadata("legacy", is_new=True, topic="old")
    sessions_dir = tmp_path / "sessions"
    (sessions_dir / "legacy.hist.json").write_text(
        json.dumps([{"role": "user", "content": "old"}]), encoding="utf-8"
    )

    assert [item["session_id"] for item in manager.list_resumable("current")] == ["valid"]
    assert manager.load_state("valid").context_messages() == valid.context_messages()
    assert not (sessions_dir / "valid.hist.json").exists()
    assert not (sessions_dir / "valid.input.json").exists()


def test_app_resume_switches_state_inside_gates_and_resets_transient_store() -> None:
    class Bus:
        def __init__(self) -> None:
            self.joins = 0

        @contextmanager
        def reject_ui_requests(self):
            yield

        async def join(self) -> None:
            self.joins += 1

    class UI:
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

    class Sessions:
        def __init__(self) -> None:
            self.saved = []

        def save_state(self, session_id, state) -> None:
            self.saved.append((session_id, state))

    class Router:
        def bind_session_state(self, state) -> None:
            self.state = state

    source = SessionState()
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
    bus = Bus()
    ui = UI()
    sessions = Sessions()
    router = Router()
    deps = SimpleNamespace(
        event_bus=bus,
        ui=ui,
        session_mgr=sessions,
        session_id="source",
        session_state=source,
        session_context=["old"],
        role_mgr=None,
        plan_mode_controller=None,
    )
    app = AgentApp(deps, AgentViewStore(), router)  # type: ignore[arg-type]
    app._install_plan_mode_controller = lambda: None  # type: ignore[method-assign]
    prompt = SimpleNamespace(invalidate_cache=lambda: None)
    fake_agent = SimpleNamespace(
        uuid="new-main",
        agent_type="main",
        llm=SimpleNamespace(model="model", reasoning_effort="medium"),
        reasoning_effort=None,
        plan_active=True,
        _task_mgr=None,
        _prompt_mgr=prompt,
        get_input_history=target.input_history,
    )
    result = ResumeResult(
        session_id="target",
        state=target,
        metadata={"topic": "主题", "plan_active": True},
    )

    async def scenario() -> None:
        with patch("src.app.app.Agent.from_manifest", return_value=fake_agent):
            agent, summary = await app.resume_session(result)
        assert agent is fake_agent
        assert "已恢复会话 target" in summary

    asyncio.run(scenario())

    assert sessions.saved == [("source", source)]
    assert deps.session_id == "target"
    assert deps.session_state is target
    assert router.state is target
    assert ui.state is target
    assert app.agent_view_store.foreground_uuid == "new-main"
    assert [s.uuid for s in app.agent_view_store.subagent_snapshots()] == ["worker-target"]
    assert deps.session_context != ["old"]
    assert bus.joins == 2
