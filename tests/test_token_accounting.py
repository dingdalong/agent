"""LLM 成功调用的会话、Agent 与核账日志回归测试。"""

from __future__ import annotations

import logging

import pytest

from src.events.types import LLMCallCompleted
from src.interfaces.agent_view_store import (
    AgentViewStore,
    ContextUsage,
    TokenUsage,
)


def _completed(
    call_id: str,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
    caller_type: str | None,
    caller_uuid: str | None,
) -> LLMCallCompleted:
    """构造一条包含完整 provider usage 的成功事件。"""
    return LLMCallCompleted(
        timestamp=1.0,
        source="provider",
        model="model-test",
        call_id=call_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        caller_agent_type=caller_type,
        caller_uuid=caller_uuid,
    )


def test_session_usage_includes_main_subagent_and_unidentified_calls() -> None:
    """会话累计覆盖所有成功调用，Agent 与前台上下文仍按 UUID 隔离。"""
    store = AgentViewStore()
    store.register_foreground("main-uuid", "main")

    store.record(_completed(
        "call-main",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cache_read_tokens=30,
        cache_creation_tokens=10,
        caller_type="main",
        caller_uuid="main-uuid",
    ))
    store.record(_completed(
        "call-worker",
        input_tokens=40,
        output_tokens=5,
        total_tokens=45,
        cache_read_tokens=8,
        cache_creation_tokens=4,
        caller_type="worker",
        caller_uuid="worker-uuid",
    ))
    store.record(_completed(
        "call-internal",
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
        cache_read_tokens=2,
        cache_creation_tokens=None,
        caller_type=None,
        caller_uuid=None,
    ))

    session = store.session_snapshot()
    assert session.usage == TokenUsage(
        input_tokens=152,
        output_tokens=28,
        cache_read_tokens=40,
    )
    assert session.foreground_context == ContextUsage(used_tokens=100)

    main = store.agent_snapshot("main-uuid")
    worker = store.agent_snapshot("worker-uuid")
    assert main is not None
    assert worker is not None
    assert main.usage == TokenUsage(100, 20, 30)
    assert worker.usage == TokenUsage(40, 5, 8)
    assert worker.context == ContextUsage(used_tokens=40)


def test_usage_log_preserves_raw_none_and_records_applied_totals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """核账日志同时保留 provider 原值、实际增量与更新后的会话累计。"""
    store = AgentViewStore()
    event = LLMCallCompleted(
        timestamp=1.0,
        source="request-body-must-not-be-logged",
        model="model-log",
        call_id="call-log",
        input_tokens=12,
        output_tokens=None,
        total_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=4,
    )

    with caplog.at_level(logging.INFO, logger="src.interfaces.agent_view_store"):
        store.record(event)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "src.interfaces.agent_view_store"
    ]
    assert messages == [
        "LLM token核账 call_id=call-log model=model-log caller_type=None "
        "caller_uuid=None raw_input_tokens=12 raw_output_tokens=None "
        "raw_total_tokens=None raw_cache_read_input_tokens=None "
        "raw_cache_creation_input_tokens=4 delta_input_tokens=12 "
        "delta_output_tokens=0 delta_total_tokens=12 "
        "delta_cache_read_input_tokens=0 session_input_tokens=12 "
        "session_output_tokens=0 session_total_tokens=12 "
        "session_cache_read_input_tokens=0"
    ]
    assert event.source not in caplog.text
