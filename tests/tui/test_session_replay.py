"""会话记录回放投影（历史条目 / 纯文本行）测试。"""

from __future__ import annotations

from rich.text import Text

from src.common.session_state import SessionRecord, ViewPayload
from src.interfaces.tui.session_replay import history_entries, plain_lines


def _plain(entry) -> str:
    """把历史条目内容还原为纯文本，便于断言。"""
    return entry.content.plain if isinstance(entry.content, Text) else entry.content


def _record(
    record_id: str,
    payload: ViewPayload | None,
    *,
    kind: str = "view",
) -> SessionRecord:
    return SessionRecord(id=record_id, timestamp=0.0, kind=kind, view=payload)


def test_history_entries_derive_unique_ids_from_record() -> None:
    """条目 id 以记录 id 为前缀并在整批回放内保持唯一。"""
    records = [
        _record("r1", ViewPayload("user", {"text": "你好"}), kind="user"),
        _record(
            "r2",
            ViewPayload("assistant", {"thinking": "想一下", "content": "回答"}),
            kind="assistant",
        ),
    ]

    entries = history_entries(records)

    ids = [entry.id for entry in entries]
    assert ids[:2] == ["r1:0", "r2:1"]
    assert len(set(ids)) == len(ids)
    assert [_plain(entry) for entry in entries] == [
        "› 你好",
        "› 思考",
        "想一下",
        "› 助手：",
        "回答",
    ]


def test_history_entries_skip_blocks_without_text() -> None:
    entries = history_entries([
        _record("r1", ViewPayload("output", {"content": ""}), kind="output"),
        _record("r2", ViewPayload("output", {"content": "有内容"}), kind="output"),
    ])

    assert [entry.id for entry in entries] == ["r2:0"]


def test_history_entries_keep_markdown_flag_for_text_blocks_only() -> None:
    entries = history_entries([
        _record("r1", ViewPayload("output", {"content": "**粗体**", "markdown": True}), kind="output"),
        _record("r2", ViewPayload("user", {"text": "纯文本"}), kind="user"),
    ])

    assert entries[0].markdown is True
    assert entries[1].markdown is False


def test_history_entries_keep_block_style_and_spacing() -> None:
    entries = history_entries([
        _record("r1", ViewPayload("assistant", {"thinking": "想一下"}), kind="assistant"),
    ])

    label, body = entries
    assert label.spacing == 0 and body.spacing == 1
    assert body.style == "#8d989f"


def test_history_entries_ignore_records_without_view() -> None:
    assert history_entries([_record("r1", None, kind="user")]) == []


def test_plain_lines_share_wording_and_drop_styles() -> None:
    records = [
        _record("r1", ViewPayload("user", {"text": "你好"}), kind="user"),
        _record("r2", ViewPayload("output", {"content": "回答", "markdown": True}), kind="output"),
    ]

    assert plain_lines(records) == ["› 你好", "回答"]
    assert all(isinstance(line, str) for line in plain_lines(records))


def test_plain_lines_skip_blank_blocks() -> None:
    assert plain_lines([_record("r1", ViewPayload("output", {"content": ""}), kind="output")]) == []
