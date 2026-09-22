"""会话追加式日志与内容池去重的契约测试。"""

from __future__ import annotations

import json

from src.common.session_log import (
    build_batch,
    content_id,
    fold_log,
    iter_batch_lines,
    text_digest,
)
from src.common.session_state import SessionState, ViewPayload
from src.mgr.session_mgr import SessionMgr


LONG_TEXT = "长文本片段。" * 60


def _log_lines(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _mark_records_dirty(state: SessionState) -> None:
    """把上下文记录标记为待重提交，用于验证日志按内容去重。"""
    state._dirty_records.update(state.context_ids)


def test_same_long_text_is_stored_once_across_batches(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "user", "content": LONG_TEXT})
    state.append_context({"role": "assistant", "content": LONG_TEXT})
    manager.append_state("session", state)

    sessions_dir = tmp_path / "sessions"
    text = (sessions_dir / "session.jsonl").read_text(encoding="utf-8")

    assert text.count(LONG_TEXT) == 1
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == [
        {"role": "user", "content": LONG_TEXT},
        {"role": "assistant", "content": LONG_TEXT},
    ]


def test_append_only_writes_one_line_per_change_and_nothing_when_clean(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    log_path = tmp_path / "sessions" / "session.jsonl"

    manager.append_state("session", state)
    assert not log_path.exists()

    state.append_user("第一条", {"role": "user", "content": "第一条"})
    manager.append_state("session", state)
    assert len(_log_lines(log_path)) == 1

    manager.append_state("session", state)
    assert len(_log_lines(log_path)) == 1

    state.append_user("第二条", {"role": "user", "content": "第二条"})
    manager.append_state("session", state)
    assert len(_log_lines(log_path)) == 2
    assert [line["seq"] for line in _log_lines(log_path)] == [1, 2]


def test_record_updates_are_folded_with_the_last_version(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    record_id = state.record_view(
        ViewPayload("assistant", {"content": "半句"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    manager.append_state("session", state)
    state.record_view(
        ViewPayload("assistant", {"content": "完整回答"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    manager.append_state("session", state)

    restored = manager.load_state("session")
    assert restored is not None
    record = next(item for item in restored.records if item.id == record_id)
    assert record.view is not None
    assert record.view.data["content"] == "半句完整回答"


def test_fold_ignores_corrupt_lines_and_keeps_missing_refs() -> None:
    state = SessionState()
    state.append_context({"role": "user", "content": LONG_TEXT})
    batch = build_batch(seq=1, timestamp=0.0, payload=state.take_commit(), pool={})
    broken = dict(batch)
    broken["records"] = [{"id": "x", "model_message": {"content": {"$ref": "missing"}}}]

    payload, contents, last_seq = fold_log(iter_batch_lines("not json\n" + json.dumps(broken)))

    assert payload["records"][0]["model_message"]["content"] == {"$ref": "missing"}
    assert contents == {content_id(LONG_TEXT): LONG_TEXT}
    assert last_seq == 1


def test_session_state_payload_round_trip_has_no_deep_copies() -> None:
    state = SessionState()
    message = {"role": "assistant", "content": LONG_TEXT}
    record_id = state.append_context(message)

    stored = next(record for record in state.records if record.id == record_id)
    assert stored.model_message is message
    assert state.context_messages()[0] is message


def test_reopened_session_reuses_pool_and_sequence(tmp_path) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_user("问题", {"role": "user", "content": LONG_TEXT})
    manager.append_state("session", state)

    reopened = SessionMgr(tmp_path, tmp_path)
    resumed = reopened.load_state("session")
    assert resumed is not None

    resumed.append_context({"role": "assistant", "content": LONG_TEXT})
    reopened.append_state("session", resumed)

    log_path = tmp_path / "sessions" / "session.jsonl"
    text = log_path.read_text(encoding="utf-8")
    assert text.count(LONG_TEXT) == 1
    assert [line["seq"] for line in _log_lines(log_path)] == [1, 2]
    restored = reopened.load_state("session")
    assert restored is not None
    assert [message["content"] for message in restored.context_messages()] == [
        LONG_TEXT,
        LONG_TEXT,
    ]


def test_failed_append_retries_content_pool_and_state(tmp_path, monkeypatch) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "user", "content": LONG_TEXT})

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.mgr.session_mgr.append_text", fail)
    assert manager.append_state("session", state) is False
    assert not (tmp_path / "sessions" / "session.jsonl").exists()

    monkeypatch.undo()
    assert manager.append_state("session", state) is True
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == [{"role": "user", "content": LONG_TEXT}]
    assert (tmp_path / "sessions" / "session.jsonl").read_text(encoding="utf-8").count(LONG_TEXT) == 1


def test_retry_after_post_write_failure_deduplicates_existing_batch(tmp_path, monkeypatch) -> None:
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "user", "content": LONG_TEXT})
    real_append = __import__("src.mgr.session_mgr", fromlist=["append_text"]).append_text
    calls = 0

    def write_then_fail(path, content, **kwargs):
        nonlocal calls
        real_append(path, content, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("fsync failed after append")

    monkeypatch.setattr("src.mgr.session_mgr.append_text", write_then_fail)
    assert manager.append_state("session", state) is False
    assert manager.append_state("session", state) is True

    lines = (tmp_path / "sessions" / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == [{"role": "user", "content": LONG_TEXT}]


def test_log_skips_records_identical_to_the_last_written_copy(tmp_path) -> None:
    """脏标记不产生重复落盘：与上次逐字节相同的记录不写进新批。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "assistant", "content": LONG_TEXT})
    manager.append_state("session", state)

    log_path = tmp_path / "sessions" / "session.jsonl"
    before = _log_lines(log_path)

    _mark_records_dirty(state)
    manager.append_state("session", state)

    assert _log_lines(log_path) == before
    assert log_path.read_text(encoding="utf-8").count(LONG_TEXT) == 1


def test_written_projection_keeps_only_content_digests(tmp_path) -> None:
    """运行期已落盘投影只保留摘要，不再持有整份编码副本。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "assistant", "content": LONG_TEXT})
    state.update_plan({"steps": [LONG_TEXT]})
    manager.append_state("session", state)

    written = manager._written["session"]
    assert written.records
    assert all(
        isinstance(digest, bytes) and len(digest) == 16
        for digest in written.records.values()
    )
    assert isinstance(written.plan, bytes) and len(written.plan) == 16
    assert LONG_TEXT not in repr(written)

    # 恢复预热后重新提交相同内容仍然零写入。
    log_path = tmp_path / "sessions" / "session.jsonl"
    before = _log_lines(log_path)
    reopened = SessionMgr(tmp_path, tmp_path)
    resumed = reopened.load_state("session")
    assert resumed is not None
    _mark_records_dirty(resumed)
    reopened.append_state("session", resumed)

    assert _log_lines(log_path) == before


def test_log_skips_context_delta_without_changes(tmp_path) -> None:
    """上下文重绑但结果相同时不写新批，也不重复登记内容池。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "assistant", "content": LONG_TEXT})
    manager.append_state("session", state)

    log_path = tmp_path / "sessions" / "session.jsonl"
    before = _log_lines(log_path)

    state.replace_context([{"role": "assistant", "content": LONG_TEXT}])
    manager.append_state("session", state)

    assert _log_lines(log_path) == before


def test_log_keeps_writing_real_changes_after_skipping(tmp_path) -> None:
    """过滤只丢弃逐字节相同的区块，真实变更仍然追加。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    record_id = state.append_context({"role": "assistant", "content": LONG_TEXT})
    manager.append_state("session", state)

    _mark_records_dirty(state)
    manager.append_state("session", state)

    record = next(item for item in state.records if item.id == record_id)
    assert record.model_message is not None
    record.model_message["content"] = LONG_TEXT + "补充"
    _mark_records_dirty(state)
    manager.append_state("session", state)

    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages()[-1]["content"] == LONG_TEXT + "补充"


def test_fold_applies_context_deltas() -> None:
    from src.common.session_log import LOG_VERSION

    payload, _contents, last_seq = fold_log([
        {"v": LOG_VERSION, "seq": 1, "ts": 0.0, "context": {"keep": 0, "append": ["a", "b"]}},
        {"v": LOG_VERSION, "seq": 2, "ts": 0.0, "context": {"keep": 2, "append": ["c"]}},
        {"v": LOG_VERSION, "seq": 3, "ts": 0.0, "context": {"keep": 1, "append": []}},
    ])

    assert payload["context_ids"] == ["a"]
    assert last_seq == 3


def test_load_state_ignores_malformed_batches(tmp_path) -> None:
    """损坏批次不会阻止当前格式日志中的有效批次恢复。"""
    from src.common.session_log import LOG_VERSION

    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "user", "content": "有效会话"})
    manager.append_state("session", state)
    log_path = tmp_path / "sessions" / "session.jsonl"
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
        stream.write(json.dumps({"v": LOG_VERSION, "seq": 0}) + "\n")

    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == [{"role": "user", "content": "有效会话"}]


def test_streamed_round_survives_restore_with_model_message(tmp_path) -> None:
    """流式回合在检查点之后绑定模型消息：增量必须把这条消息带到磁盘上。

    回归场景：增量只带正文时，assistant 记录在磁盘上永远没有 model_message，
    恢复校验会判定整份日志损坏，会话彻底无法恢复。
    """
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_user("问题", {"role": "user", "content": "问题"})
    manager.append_state("session", state)
    for index, chunk in enumerate(("第一段", "第二段")):
        state.record_view(
            ViewPayload("assistant", {"content": chunk}),
            kind="assistant",
            correlation_id="call-1",
            merge=True,
        )
        if index == 0:
            manager.append_state("session", state)

    message = {"role": "assistant", "content": "第一段第二段"}
    state.bind_model_message(message, correlation_id="call-1", kind="assistant")
    manager.append_state("session", state)

    log_path = tmp_path / "sessions" / "session.jsonl"
    deltas = [delta for line in _log_lines(log_path) for delta in line.get("record_deltas") or []]
    assert any((delta.get("fields") or {}).get("model_message") == message for delta in deltas)

    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == state.context_messages()
    record = next(item for item in restored.records if item.kind == "assistant")
    assert record.model_message == message
    assert record.view is not None
    assert record.view.data["content"] == "第一段第二段"


def test_streamed_round_appends_body_without_rewriting_record(tmp_path) -> None:
    """流式正文只在首份快照与增量里各出现一次，记录不会被整份重写。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    chunks = tuple("片段" * 200 + str(index) for index in range(4))
    for chunk in chunks:
        state.record_view(
            ViewPayload("assistant", {"content": chunk}),
            kind="assistant",
            correlation_id="call-1",
            merge=True,
        )
        manager.append_state("session", state)

    log_path = tmp_path / "sessions" / "session.jsonl"
    lines = _log_lines(log_path)
    snapshots = [record for line in lines for record in line.get("records") or []]
    appends = [
        delta["view"]["append"]["content"]
        for line in lines
        for delta in line.get("record_deltas") or []
    ]
    assert [record["id"] for record in snapshots] == [state.records[0].id]
    assert len(appends) == len(chunks) - 1
    assert all(item for item in appends)
    full = "".join(chunks)
    pooled = sum(
        len(text) for line in lines for text in (line.get("contents") or {}).values()
    )
    assert pooled == len(full)

    restored = manager.load_state("session")
    assert restored is not None
    assert restored.records[0].view.data["content"] == full


def test_chunk_arriving_during_write_lands_in_the_log(tmp_path) -> None:
    """写入期间到达的正文必须写进日志，而不是留在内存里等下一次变更。"""
    manager = SessionMgr(tmp_path, tmp_path)
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
    assert manager.append_batch("session", commit.payload)
    state.ack_commit(commit)

    assert manager.append_state("session", state)
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.records[0].view.data["content"] == "第一批追加"


def test_stream_base_mismatch_keeps_body_and_requests_full_snapshot(tmp_path) -> None:
    """写侧基线对不上时保留正文并登记整份重写，已展示内容不会在磁盘上被截断。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "首段"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    manager.append_state("session", state)
    state.record_view(
        ViewPayload("assistant", {"content": "追加正文"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    commit = state.prepare_commit()
    assert commit is not None
    delta = commit.payload["record_deltas"][0]
    delta["stream_boundaries"]["content"]["base"] = text_digest("错位基线")

    record_id = state.records[0].id
    assert manager.append_batch("session", commit.payload, commit.resync) is True

    assert commit.resync == {record_id}
    log_path = tmp_path / "sessions" / "session.jsonl"
    appends = [
        item["view"]["append"]["content"]
        for line in _log_lines(log_path)
        for item in line.get("record_deltas") or []
    ]
    assert "追加正文" in "".join(appends)

    # 无法对位的正文按整份快照重写，磁盘与内存收敛到同一份完整文本。
    state.ack_commit(commit)
    assert manager.append_state("session", state)
    lines = _log_lines(log_path)
    snapshots = [record for line in lines for record in line.get("records") or []]
    assert snapshots[-1]["id"] == record_id
    assert snapshots[-1]["view"]["data"]["content"] == "首段追加正文"
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.records[0].view.data["content"] == "首段追加正文"

    # 同一份增量重复提交仍然是零写入。
    assert manager.append_batch("session", commit.payload) is True
    assert len(_log_lines(log_path)) == len(lines)


def test_view_key_removal_falls_back_to_full_snapshot(tmp_path) -> None:
    """视图被整份替换、键集合收缩时回退整份快照，避免旧键留在磁盘上。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "正文", "extra": {"old": True}}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    manager.append_state("session", state)
    state.record_view(
        ViewPayload("assistant", {"content": "新正文"}),
        kind="assistant",
        correlation_id="call-1",
        merge=False,
    )
    manager.append_state("session", state)

    lines = _log_lines(tmp_path / "sessions" / "session.jsonl")
    assert lines[-1].get("records") and not lines[-1].get("record_deltas")
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.records[0].view.data == {"content": "新正文"}
    assert restored.records[0].view.data == state.records[0].view.data


def test_unchanged_context_rewrite_writes_nothing(tmp_path) -> None:
    """provider 归一化回写：内容没变时不写记录也不写增量。"""
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    state.append_context({"role": "assistant", "content": "答案"})
    manager.append_state("session", state)
    log_path = tmp_path / "sessions" / "session.jsonl"
    before = log_path.read_text(encoding="utf-8")

    _mark_records_dirty(state)
    manager.append_state("session", state)

    assert log_path.read_text(encoding="utf-8") == before

class _RewritingGuard:
    """模拟入口脱敏漏网的测试保护器：落盘前把密钥改写成占位符。"""

    def redact(self, value: object) -> object:
        """递归改写载荷中的敏感正文。"""
        if isinstance(value, str):
            return value.replace("sk-leak", "[REDACTED]")
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        return value


def test_write_side_leak_keeps_redacted_snapshot_and_resyncs_record(tmp_path) -> None:
    """写侧发现漏网敏感文本：整份快照按脱敏结果写入，错位增量丢弃并登记重写。"""
    state = SessionState()
    state.record_view(
        ViewPayload("assistant", {"content": "首段"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    clean_manager = SessionMgr(tmp_path, tmp_path)
    clean_manager.append_state("session", state)

    state.record_view(
        ViewPayload("assistant", {"content": "泄漏 sk-leak 的正文"}),
        kind="assistant",
        correlation_id="call-1",
        merge=True,
    )
    commit = state.prepare_commit()
    assert commit is not None
    assert commit.payload["record_deltas"]

    manager = SessionMgr(tmp_path, tmp_path, data_guard=_RewritingGuard())
    assert manager.append_batch("session", commit.payload, commit.resync) is True

    record_id = state.records[0].id
    assert commit.resync == {record_id}
    log_path = tmp_path / "sessions" / "session.jsonl"
    text = log_path.read_text(encoding="utf-8")
    assert "sk-leak" not in text
    lines = _log_lines(log_path)
    deltas = [delta for line in lines for delta in line.get("record_deltas") or []]
    assert all(delta.get("id") != record_id for delta in deltas)

    # 下一轮按整份快照收敛：ack 后重新提交，磁盘与内存都只有脱敏文本。
    state.ack_commit(commit)
    assert manager.append_state("session", state)
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.records[0].view.data["content"] == "首段泄漏 [REDACTED] 的正文"
    assert "sk-leak" not in log_path.read_text(encoding="utf-8")
