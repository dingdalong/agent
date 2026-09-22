from __future__ import annotations

import asyncio
import json
import logging
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import Agent
from src.common.session_log import fold_log, iter_batch_lines
from src.common.data_guard import DataGuard, REDACTED
from src.events.types import ResponseDelta
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.output_router import OutputRouter
from src.mgr.hooks_mgr import HookRunResult, HooksMgr
from src.mgr.memory_mgr import MemoryMgr
from src.mgr.session_mgr import SessionMgr
from src.common.session_state import SessionState, ViewPayload
from src.mgr.task_mgr import TaskManager
from src.mgr.process_mgr import ProcessMgr
from src.common.sandbox import ExecutionPolicy


SECRET = "sentinel-secret-value"


def _guard() -> DataGuard:
    return DataGuard({"provider": SECRET})


def test_session_memory_and_task_persistence_redact_and_use_owner_only_files(tmp_path):
    guard = _guard()
    global_dir = tmp_path / "global"
    workdir = tmp_path / "work"
    workdir.mkdir()

    sessions = SessionMgr(global_dir, workdir, guard)
    state = SessionState()
    state.append_context({"role": "assistant", "content": SECRET})
    sessions.append_state("session", state)
    sessions.save_metadata("session", is_new=True, topic=f"topic {SECRET}")

    memory = MemoryMgr(workdir, data_guard=guard)
    memory.save("security", f"description {SECRET}", "project", f"body {SECRET}")

    tasks = TaskManager(global_dir / "tasks" / "session", data_guard=guard)
    tasks.create(
        f"subject {SECRET}",
        f"description {SECRET}",
        metadata={"value": SECRET},
    )

    files = [
        global_dir / "sessions" / "session.jsonl",
        global_dir / "sessions" / "session.json",
        next((workdir / ".agent" / "memory").glob("*.md")),
        global_dir / "tasks" / "session" / "1.json",
        global_dir / "tasks" / "session" / ".highwatermark",
    ]
    for path in files:
        assert SECRET not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert SECRET not in repr(sessions.load_state("session"))
    assert SECRET not in memory.read("security")
    assert SECRET not in repr(tasks.get_task("1"))


def test_agent_history_redacts_assistant_tool_call_arguments():
    agent = object.__new__(Agent)
    agent.deps = SimpleNamespace(data_guard=_guard())
    history: list[dict] = []

    agent._append_message(history, {
        "role": "assistant",
        "tool_calls": [{
            "function": {"arguments": json.dumps({"value": SECRET})},
        }],
    })

    assert SECRET not in repr(history)
    assert REDACTED in repr(history)


def test_hook_output_is_redacted_but_trusted_pretool_update_stays_raw(tmp_path):
    manager = HooksMgr(tmp_path, data_guard=_guard(), base_environment={})
    result = HookRunResult(
        additional_context=[SECRET],
        permission_decisions=[("deny", SECRET)],
        updated_input={"value": SECRET},
        blocked=True,
        block_reason=SECRET,
        errors=[SECRET],
    )

    safe = manager._sanitize_result(result, keep_updated_input=True)

    assert safe.updated_input == {"value": SECRET}
    assert SECRET not in repr((
        safe.additional_context,
        safe.permission_decisions,
        safe.block_reason,
        safe.errors,
    ))


@pytest.mark.integration
def test_shell_timeout_reaps_background_process_group(tmp_path):
    deps = SimpleNamespace(
        workdir=tmp_path,
        data_guard=DataGuard(),
        config_mgr=SimpleNamespace(environment={"PATH": "/bin:/usr/bin"}),
    )
    started = time.monotonic()

    async def scenario():
        manager = ProcessMgr()
        result = await manager.start(('test', 'main'), 'sleep 30 &', tmp_path, {}, ExecutionPolicy('sleep 30 &', tmp_path, tmp_path), 100, 1000)
        await manager.close()
        return result

    result = asyncio.run(scenario())
    assert result.error_code == 'timeout'
    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    "text",
    [
        "http://127.0.0.1:port`",
        "http://host:-1",
        "http://host:99999999",
        "see http://localhost:port`/api?token=abc&a=b",
    ],
)
def test_redact_tolerates_non_numeric_or_out_of_range_ports(text):
    result = DataGuard().redact(text)

    assert isinstance(result, str)
    assert "abc" not in result


def test_redact_non_numeric_port_keeps_host_and_redacts_query_values():
    result = DataGuard().redact("see http://localhost:port`/api?token=abc&a=b")

    assert result == "see http://localhost/api?token=%5BREDACTED%5D&a=b"


def test_shell_summary_tolerates_non_numeric_port():
    result = DataGuard().shell_summary("curl -sS http://127.0.0.1:port`/x?token=abc")

    assert result == "curl -sS '<url>'"
    assert "abc" not in result


def test_redact_unparseable_url_falls_back_to_placeholder():
    assert DataGuard().redact("http://[::1?q=supersecret") == "<url>"


def test_redact_valid_url_output_is_unchanged():
    result = DataGuard().redact("https://user:pass@example.com:8443/path?token=x&a=b#frag")

    assert result == "https://example.com:8443/path?token=%5BREDACTED%5D&a=b#frag"


@pytest.mark.parametrize(
    "text",
    [
        "credits/token = 100",
        "price per token = 0.000025",
        "每 token = 0.000025 元",
        "100 credits/token = 0.4",
        "元/token = 1e-8",
        "per token: ¥0.000025",
        "price per token = 1,000,000",
    ],
)
def test_redact_keeps_measurement_assignments(text):
    """计量与单价写法里的 token 只是单位名，脱敏不得误伤这些正常文本。"""
    assert DataGuard().redact(text) == text
    assert DataGuard().contains_secret(text) is False


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("token=abc123def456", "abc123def456"),
        ("password: hunter2", "hunter2"),
        ("api_key = sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
        ("Authorization: Bearer abc123def456", "abc123def456"),
        ("https://x.com/a/token=abc", "abc"),
        ("credits/token = hunter2", "hunter2"),
        ("per token = hunter2", "hunter2"),
    ],
)
def test_redact_still_covers_credential_assignments(text, secret):
    result = DataGuard().redact(text)

    assert REDACTED in result
    assert secret not in result


def test_writer_detects_leaked_secret_and_warns(tmp_path, caplog):
    """绕过入口写入的敏感文本由写侧兜底脱敏，并留下入口漏路径的告警。"""
    guard = _guard()
    manager = SessionMgr(tmp_path, tmp_path, guard)
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": f"泄漏正文 {SECRET}"}),
        kind="assistant",
    )

    with caplog.at_level(logging.WARNING, logger="src.mgr.session_mgr"):
        assert manager.append_state("session", state)

    log_path = tmp_path / "sessions" / "session.jsonl"
    assert SECRET not in log_path.read_text(encoding="utf-8")
    assert REDACTED in log_path.read_text(encoding="utf-8")
    assert "落盘前仍需脱敏" in caplog.text


def test_streamed_text_is_redacted_on_every_surface(tmp_path):
    """脱敏发生在文本入口：显示、内存、磁盘与下一轮请求共用同一份脱敏文本。"""
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    guard = DataGuard({"provider": secret})
    ui_events: list[object] = []

    class _UI:
        async def on_event(self, event: object) -> None:
            ui_events.append(event)

    state = SessionState()
    store = AgentViewStore()
    store.register_foreground("main-id", "main")
    router = OutputRouter(_UI(), store, session_state=state, data_guard=guard)

    # 秘密被拆成两块流式增量：分块脱敏看不见，整段复核才能改写。
    async def scenario() -> None:
        for chunk in (f"开头 {secret[:14]}", f"{secret[14:]} 结尾"):
            await router.dispatch(ResponseDelta(
                timestamp=1.0, source="model", content=chunk, call_id="call-1",
                caller_agent_type="main", caller_uuid="main-id",
            ))

    asyncio.run(scenario())
    assert state.settle_view_text(guard.redact)
    full_text = f"开头 {secret} 结尾"
    state.bind_model_message(
        guard.redact({"role": "assistant", "content": full_text}),
        correlation_id="call-1",
        kind="assistant",
    )

    manager = SessionMgr(tmp_path, tmp_path, guard)
    assert manager.append_state("session", state)
    payload, _contents, _seq = fold_log(
        iter_batch_lines((tmp_path / "sessions" / "session.jsonl").read_text(encoding="utf-8")),
    )
    restored = SessionState.from_dict(payload)
    assert restored is not None

    display = state.visible_records()[0].view.data["content"]
    memory = state.records[0].view.data["content"]
    disk = restored.records[0].view.data["content"]
    request = state.context_messages()[0]["content"]
    assert display == memory == disk == request == f"开头 {REDACTED} 结尾"
    assert secret not in "".join(str(event) for event in ui_events)
