"""事件与视图到展示块的投影契约测试。"""

from __future__ import annotations

from src.common.session_state import SessionRecord, ViewPayload
from src.events.types import (
    CompactDelta,
    LLMCallFailed,
    LLMLengthRetrying,
    LLMRetrying,
    PermissionNotice,
    ResponseDelta,
)
from src.interfaces.tui.event_lines import blocks_for_event, blocks_for_view


def _record(
    record_id: str,
    payload: ViewPayload | None,
    *,
    kind: str = "view",
) -> SessionRecord:
    return SessionRecord(id=record_id, timestamp=0.0, kind=kind, view=payload)


def test_retry_line_waits_for_partial_marker_or_countdown() -> None:
    """TTY 的倒计时在状态栏，只有非 TTY 传 countdown 时才产出行。"""
    event = LLMRetrying(
        timestamp=1.0,
        source="test",
        error_kind="timeout",
        safe_message="连接超时",
        attempt=1,
        max_attempts=3,
    )

    assert blocks_for_event(event) == []
    blocks = blocks_for_event(event, retry_countdown=5)
    assert [block.plain for block in blocks] == [
        "⚠ 尝试 1/3 失败，将重试 [timeout] 连接超时；5秒后重试"
    ]
    assert blocks[0].content.style == "yellow"


def test_partial_retry_marks_discarded_fragment() -> None:
    blocks = blocks_for_event(LLMRetrying(
        timestamp=1.0,
        source="test",
        error_kind="network",
        safe_message="连接中断",
        partial=True,
        tool_fragment_state="partial",
        attempt=2,
        max_attempts=4,
    ))

    assert [block.plain for block in blocks] == [
        "⚠ 尝试 2/4 失败，将重试 [network] 连接中断 (partial=true, tool=partial)"
    ]


def test_length_retry_maps_truncation_kind_and_strategy() -> None:
    def line(kind: str, strategy: str) -> str:
        blocks = blocks_for_event(LLMLengthRetrying(
            timestamp=1.0,
            source="test",
            truncation_kind=kind,
            strategy=strategy,
            effort="low",
            attempt=1,
            max_attempts=2,
        ))
        return blocks[0].plain

    assert line("tool_call", "continue") == "⚠ 输出截断（工具调用）：从中断处继续生成 (1/2)"
    assert line("content", "regenerate-lower-effort") == (
        "⚠ 输出截断（正文）：降低推理力度至 low 后重生成 (1/2)"
    )
    assert line("thinking", "regenerate-compress") == "⚠ 输出截断（思考）：压缩思考后重生成 (1/2)"
    assert line("unknown", "continue") == "⚠ 输出截断（未知）：从中断处继续生成 (1/2)"
    assert line("tool_call", "unexpected") == "⚠ 输出截断（工具调用）：从中断处继续生成 (1/2)"


def test_terminal_failure_keeps_safe_identifiers() -> None:
    blocks = blocks_for_event(LLMCallFailed(
        timestamp=1.0,
        source="test",
        error_kind="auth",
        safe_message="鉴权失败",
        attempts=3,
        request_id="req-1",
        diagnostic_id="diag-2",
    ))

    assert [block.plain for block in blocks] == [
        "✘ LLM 调用失败 [auth] 鉴权失败 (request_id=req-1, diagnostic_id=diag-2)"
    ]
    assert blocks[0].content.style == "red"

    without_ids = blocks_for_event(LLMCallFailed(
        timestamp=1.0,
        source="test",
        error_kind="auth",
        safe_message="鉴权失败",
        attempts=3,
    ))
    assert [block.plain for block in without_ids] == ["✘ LLM 调用失败 [auth] 鉴权失败"]


def test_compact_delta_prefixes_caller_label() -> None:
    blocks = blocks_for_event(CompactDelta(
        timestamp=1.0,
        source="test",
        content=" 已压缩 12 条消息 ",
        caller_agent_type="worker",
        caller_uuid="abcdef12-3456-7890",
    ))

    assert [block.plain for block in blocks] == ["[compact] worker abcdef12 已压缩 12 条消息"]
    assert blocks[0].content.style == "bright_black"


def test_permission_notice_uses_verdict_style() -> None:
    allow = blocks_for_event(PermissionNotice(
        timestamp=1.0,
        source="test",
        status="allow",
        tool_name="read_file",
        detail="读取配置",
        decision_source="policy",
    ))
    deny = blocks_for_event(PermissionNotice(
        timestamp=1.0,
        source="test",
        status="deny",
        tool_name="write_file",
        detail="",
        decision_source="plan",
    ))

    assert allow[0].plain.startswith("✔ 策略放行 · ")
    assert "已放行" in allow[0].plain and "(读取配置)" in allow[0].plain
    assert deny[0].plain.startswith("✘ 计划模式 · ")
    assert "已拒绝" in deny[0].plain
    assert allow[0].content.style != deny[0].content.style


def test_events_without_a_projection_yield_no_block() -> None:
    """流式增量由 App 的流式通道直接消费，不产出具名行。"""
    assert blocks_for_event(ResponseDelta(timestamp=1.0, source="test", content="hi")) == []


def test_view_without_payload_yields_no_block() -> None:
    assert blocks_for_view(_record("r1", None, kind="user")) == []


def test_user_view_prefixes_each_line() -> None:
    blocks = blocks_for_view(_record(
        "r1",
        ViewPayload("user", {"text": "第一行\n第二行"}),
        kind="user",
    ))

    assert [block.plain for block in blocks] == ["› 第一行\n  第二行"]
    assert blocks[0].markdown is False


def test_assistant_view_labels_thinking_and_response() -> None:
    blocks = blocks_for_view(_record(
        "r1",
        ViewPayload("assistant", {
            "thinking": "想一下",
            "content": "答案",
            "caller_agent_type": "worker",
            "caller_uuid": "abcdef12-3456",
        }),
        kind="assistant",
    ))

    assert [block.plain for block in blocks] == [
        "› 思考(worker abcdef12)：",
        "想一下",
        "› 回复(worker abcdef12)：",
        "答案",
    ]
    assert blocks[0].spacing == 0 and blocks[2].spacing == 0
    assert blocks[1].style == "#8d989f"
    assert blocks[1].markdown is True


def test_assistant_view_keeps_partial_retry_marker() -> None:
    blocks = blocks_for_view(_record(
        "r1",
        ViewPayload("assistant", {
            "content": "半段正文",
            "event_type": "llm_retrying",
            "error_kind": "network",
            "safe_message": "连接中断",
            "partial": True,
            "tool_fragment_state": "none",
            "attempt": 1,
            "max_attempts": 3,
        }),
        kind="assistant",
    ))

    assert [block.plain for block in blocks][-1] == (
        "⚠ 尝试 1/3 失败，将重试 [network] 连接中断 (partial=true, tool=none)"
    )


def test_tool_view_renders_completed_title_and_duration() -> None:
    blocks = blocks_for_view(_record(
        "r1",
        ViewPayload("tool", {
            "started": {
                "tool_name": "read_file",
                "tool_call_id": "call-1",
                "detail": "src/app.py",
            },
            "completed": {
                "tool_name": "read_file",
                "tool_call_id": "call-1",
                "status": "success",
                "duration_seconds": 1.5,
                "result_preview": "ok",
            },
        }),
        kind="tool",
    ))

    assert len(blocks) == 1
    assert blocks[0].plain.startswith("✔ read_file src/app.py  (1.50s)")


def test_event_view_projects_compact_and_permission() -> None:
    compact = blocks_for_view(_record(
        "r1",
        ViewPayload("event", {"event_type": "compact_delta", "content": "压缩完成"}),
    ))
    permission = blocks_for_view(_record(
        "r2",
        ViewPayload("event", {
            "event_type": "permission_notice",
            "status": "deny",
            "tool_name": "write_file",
            "detail": "越界写入",
            "decision_source": "hard_rule",
        }),
    ))

    assert [block.plain for block in compact] == ["[compact] 压缩完成"]
    assert permission[0].plain.startswith("✘ 硬规则 · ")
    assert "(越界写入)" in permission[0].plain


def test_output_view_carries_markdown_flag() -> None:
    plain = blocks_for_view(_record(
        "r1",
        ViewPayload("output", {"content": "**粗体**", "markdown": False}),
        kind="output",
    ))
    markdown = blocks_for_view(_record(
        "r2",
        ViewPayload("output", {"content": "**粗体**", "markdown": True}),
        kind="output",
    ))

    assert plain[0].plain == "**粗体**" and plain[0].markdown is False
    assert markdown[0].markdown is True


def test_unknown_view_kind_yields_no_block() -> None:
    assert blocks_for_view(_record("r1", ViewPayload("subagent", {"uuid": "u1"}))) == []
