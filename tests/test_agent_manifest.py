"""Agent manifest 相关纯逻辑测试：锁死主 agent 记忆范围回归。

背景：`Agent.from_manifest` 曾直接 `memory=manifest.memory`，role.md 未声明
`memory:` 时主 agent 记忆范围被置为 None，导致 prompt_mgr 跳过项目记忆注入。
现由 `_resolve_memory_scope` 兜底：主 agent 缺省 "project"，子 agent 缺省 None。
"""

from __future__ import annotations

from unittest.mock import patch

from src.agent.agent import Agent, _resolve_memory_scope
from src.mode import RunMode


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


def test_from_manifest_without_manifest_still_applies_overrides():
    """manifest 为 None 的回退分支同样应用 overrides，不静默丢弃。"""
    deps = object()
    with patch.object(Agent, "__post_init__", lambda self: None):
        agent = Agent.from_manifest(
            None,
            deps,
            model="provider/model",
            features={"task"},
            mode=RunMode.PLAN,
        )

    assert agent.agent_type == "agent"
    assert agent.deps is deps
    assert agent.memory == "project"
    assert agent.model == "provider/model"
    assert agent.features == {"task"}
    assert agent.mode is RunMode.PLAN
