"""assistant 载体具体化与「载体唯一权威」不变式的契约测试。"""

from __future__ import annotations

from src.llm.anthropic import AnthropicProvider
from src.llm.message_view import (
    has_carrier,
    materialize_assistant,
    materialize_messages,
)


ANTHROPIC_CARRIER = {
    "role": "assistant",
    "_anthropic_content": [
        {"type": "thinking", "thinking": "先想一想"},
        {"type": "text", "text": "答案"},
        {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "a.py"}},
    ],
}

RESPONSES_CARRIER = {
    "role": "assistant",
    "_response_output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "先想一想"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path": "a.py"}',
        },
    ],
}


def test_materialize_rebuilds_derived_fields_from_both_carriers() -> None:
    for carrier in (ANTHROPIC_CARRIER, RESPONSES_CARRIER):
        view = materialize_assistant(carrier)
        assert view["content"] == "答案"
        assert view["reasoning_content"] == "先想一想"
        assert view["tool_calls"] == [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }
        ]
        assert "_anthropic_content" not in view and "_response_output" not in view


def test_materialize_keeps_plain_messages_identical() -> None:
    message = {"role": "assistant", "content": "答案", "tool_calls": []}
    assert materialize_assistant(message) is message
    assert materialize_messages([message]) == [message]
    assert not has_carrier(message)


def test_normalize_strips_derived_fields_and_keeps_tool_pairing() -> None:
    provider = object.__new__(AnthropicProvider)
    messages = [
        ANTHROPIC_CARRIER,
        {"role": "tool", "tool_call_id": "call-1", "content": "文件内容"},
    ]

    normalized = provider.normalize_messages(messages)

    assert normalized[0] == {"role": "assistant", "_anthropic_content": ANTHROPIC_CARRIER["_anthropic_content"]}
    assert normalized[1] == messages[1]


def test_normalize_drops_carrier_with_unpaired_tool_calls() -> None:
    provider = object.__new__(AnthropicProvider)

    normalized = provider.normalize_messages([ANTHROPIC_CARRIER])

    # 无 tool 响应的工具调用组非法：丢弃工具调用，但保留可见正文。
    assert normalized == [{"role": "assistant", "content": "答案"}]

