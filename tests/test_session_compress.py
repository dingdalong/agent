"""会话日志冷压缩与透明读取的契约测试。"""

from __future__ import annotations

import json
import time

from src.common.session_compress import (
    COLD_LOG_AGE_SECONDS,
    MARKER_NAME,
    cold_candidates,
    compress_log,
    decompress_log,
    run_cold_compression,
)
from src.common.session_log import LOG_VERSION
from src.common.session_state import SessionState
from src.mgr.session_mgr import SessionMgr


def test_compress_round_trip_replaces_active_log(tmp_path) -> None:
    log_path = tmp_path / "session.jsonl"
    log_path.write_text('{"v": %d, "seq": 1}\n' % LOG_VERSION, encoding="utf-8")

    archive = compress_log(log_path)

    assert archive is not None
    assert archive.exists()
    assert archive.name.startswith("session.jsonl.")
    assert not log_path.exists()
    assert decompress_log(archive) == '{"v": %d, "seq": 1}\n' % LOG_VERSION


def test_cold_candidates_respect_age_threshold(tmp_path) -> None:
    fresh = tmp_path / "fresh.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")
    stale = tmp_path / "stale.jsonl"
    stale.write_text("{}\n", encoding="utf-8")

    now = time.time()
    old = now - COLD_LOG_AGE_SECONDS - 60
    import os

    os.utime(stale, (old, old))
    os.utime(fresh, (now, now))

    assert [path.name for path in cold_candidates(tmp_path, now=now)] == ["stale.jsonl"]


def test_run_cold_compression_compresses_and_marks(tmp_path) -> None:
    log_path = tmp_path / "session.jsonl"
    log_path.write_text('{"v": %d, "seq": 1}\n' % LOG_VERSION, encoding="utf-8")
    now = time.time() + COLD_LOG_AGE_SECONDS + 60

    assert run_cold_compression(tmp_path, now=now) == 1
    assert (tmp_path / MARKER_NAME).exists()
    assert not log_path.exists()
    assert list(tmp_path.glob("session.jsonl.*"))


def test_session_manager_reads_archived_log_transparently(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_user("归档会话", {"role": "user", "content": "归档会话"})
    manager.save_metadata("session", is_new=True, topic="归档会话")
    manager.append_state("session", state)
    log_path = tmp_path / "sessions" / "session.jsonl"
    text = log_path.read_text(encoding="utf-8")

    archive = compress_log(log_path)
    assert archive is not None
    assert not log_path.exists()

    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == [{"role": "user", "content": "归档会话"}]
    assert [item["session_id"] for item in manager.list_resumable("current")] == ["session"]
    assert json.loads(text.splitlines()[0])["v"] == LOG_VERSION


def test_append_after_archive_keeps_both_generations(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_user("归档前", {"role": "user", "content": "归档前"})
    manager.append_state("session", state)
    log_path = tmp_path / "sessions" / "session.jsonl"

    assert compress_log(log_path) is not None

    resumed = manager.load_state("session")
    assert resumed is not None
    resumed.append_user("归档后", {"role": "user", "content": "归档后"})
    manager.append_state("session", resumed)

    restored = manager.load_state("session")
    assert restored is not None
    assert [message["content"] for message in restored.context_messages()] == ["归档前", "归档后"]
