from __future__ import annotations

import asyncio
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from src.agent import Agent
from src.mgr.compact_mgr import CompactMgr
from src.mgr.data_guard import DataGuard, REDACTED
from src.mgr.hooks_mgr import HookRunResult, HooksMgr
from src.mgr.memory_mgr import MemoryMgr
from src.mgr.session_mgr import SessionMgr
from src.mgr.session_state import SessionState
from src.mgr.task_mgr import TaskManager
from src.tools.builtin.shell import shell


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
    sessions.save_state("session", state)
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
        global_dir / "sessions" / "session.state.json",
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


def test_compact_transcript_and_input_are_redacted(tmp_path):
    guard = _guard()
    manager = CompactMgr(llm=SimpleNamespace(), workdir=tmp_path, data_guard=guard)
    messages = [{
        "role": "assistant",
        "tool_calls": [{"arguments": json.dumps({"token": SECRET})}],
    }]

    path = asyncio.run(manager.write_transcript(messages))

    assert SECRET not in path.read_text()
    assert REDACTED in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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


def test_shell_timeout_reaps_background_process_group(tmp_path):
    deps = SimpleNamespace(
        workdir=tmp_path,
        data_guard=DataGuard(),
        config_mgr=SimpleNamespace(environment={"PATH": "/bin:/usr/bin"}),
    )
    started = time.monotonic()

    result = asyncio.run(shell("sleep 30 &", 1, deps))

    assert result == "命令超时（1秒）"
    assert time.monotonic() - started < 3
