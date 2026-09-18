"""上下文压缩的初始化前缀、真实用户轮次和模式边界测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.mgr.compact_mgr import CompactMgr
from src.prompt_tags import PromptTag, render_prompt_tag


class _Provider:
    """提供确定 token 估算和摘要结果的最小 Provider。"""

    context_limit = 10_000

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def estimate_tokens(self, messages, prompt=None, tools=None) -> int:
        del prompt, tools
        return len(messages)

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(content="MIDDLE_SUMMARY")


def _mode(name: str, reminder: str = "") -> dict:
    sections = [render_prompt_tag(PromptTag.COLLABORATION_MODE, name)]
    if reminder:
        sections.append(render_prompt_tag(PromptTag.REMINDER, reminder))
    return {"role": "developer", "content": "\n\n".join(sections)}


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "BOOTSTRAP_EXTERNAL_CONTEXT"},
        _mode("MODE_EXECUTE", "INITIAL_REMINDER") | {
            "content": "MANAGER_WORKFLOW\n\n" + _mode(
                "MODE_EXECUTE", "INITIAL_REMINDER",
            )["content"],
        },
        {"role": "user", "content": "FIRST_REAL_REQUEST"},
        {"role": "assistant", "content": "FIRST_RESPONSE"},
        _mode("MODE_PLAN", "MODE_SWITCH_REMINDER"),
        {"role": "user", "content": "MODE_SWITCH_REQUEST"},
        {"role": "assistant", "content": "MIDDLE_RESPONSE"},
        {"role": "developer", "content": "CURRENT_REMINDER"},
        {"role": "user", "content": "CURRENT_REQUEST"},
    ]


def test_partition_preserves_bootstrap_first_request_and_latest_mode(tmp_path) -> None:
    """初始化前缀和首个真实轮次保留，最新模式移动到当前上下文。"""
    manager = CompactMgr(
        llm=_Provider(),
        workdir=tmp_path,
        keep_recent_user_turns=1,
        recent_messages_token_limit=2,
    )

    partition = manager.split_history_for_compaction(
        _history(), bootstrap_message_count=1,
    )

    assert [message["content"] for message in partition.prefix_messages] == [
        "BOOTSTRAP_EXTERNAL_CONTEXT",
        _history()[1]["content"],
        "FIRST_REAL_REQUEST",
    ]
    assert [message["content"] for message in partition.messages_to_summarize] == [
        "FIRST_RESPONSE",
        "MODE_SWITCH_REQUEST",
        "MIDDLE_RESPONSE",
    ]
    assert [message["content"] for message in partition.recent_messages] == [
        _history()[4]["content"],
        "CURRENT_REMINDER",
        "CURRENT_REQUEST",
    ]


def test_compact_history_keeps_framework_roles_outside_summary(tmp_path) -> None:
    """压缩结果按原角色保留首轮和当前模式，旧 developer 不进入摘要。"""
    provider = _Provider()
    manager = CompactMgr(
        llm=provider,
        workdir=tmp_path,
        keep_recent_user_turns=1,
        recent_messages_token_limit=2,
    )

    result = asyncio.run(manager.compact_history(
        _history(), bootstrap_message_count=1,
    ))

    assert [message["role"] for message in result.messages] == [
        "user", "developer", "user", "user", "developer", "developer", "user",
    ]
    assert result.messages[0]["content"] == "BOOTSTRAP_EXTERNAL_CONTEXT"
    assert "MANAGER_WORKFLOW" in result.messages[1]["content"]
    assert result.messages[2]["content"] == "FIRST_REAL_REQUEST"
    assert "<compacted_history_summary>" in result.messages[3]["content"]
    assert "MODE_PLAN" in result.messages[4]["content"]
    assert result.messages[-1]["content"] == "CURRENT_REQUEST"

    summary_input = provider.requests[0]["messages"][0]["content"]
    history_section = summary_input.split("<history_to_summarize ", 1)[1]
    history_section = history_section.split("</history_to_summarize>", 1)[0]
    assert "FIRST_RESPONSE" in history_section
    assert "MODE_SWITCH_REQUEST" in history_section
    assert "MODE_PLAN" not in history_section
    assert "CURRENT_REMINDER" not in history_section
