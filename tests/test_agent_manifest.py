"""Agent manifest 相关纯逻辑测试：锁死主 agent 记忆范围回归。

背景：`Agent.from_manifest` 曾直接 `memory=manifest.memory`，role.md 未声明
`memory:` 时主 agent 记忆范围被置为 None，导致 prompt_mgr 跳过项目记忆注入。
现由 `_resolve_memory_scope` 兜底：主 agent 缺省 "project"，子 agent 缺省 None。
"""

from __future__ import annotations

from src.agent.agent import _resolve_memory_scope


def test_main_agent_defaults_to_project_memory():
    """主 agent 未声明 memory 时默认加载项目记忆（回归点）。"""
    assert _resolve_memory_scope(None, is_subagent=False) == "project"


def test_subagent_defaults_to_no_memory():
    """子 agent 未声明 memory 时默认不加载项目记忆。"""
    assert _resolve_memory_scope(None, is_subagent=True) is None


def test_explicit_memory_is_passed_through():
    """显式声明的 memory 无论主/子 agent 均透传。"""
    assert _resolve_memory_scope("session", is_subagent=False) == "session"
    assert _resolve_memory_scope("project", is_subagent=True) == "project"
