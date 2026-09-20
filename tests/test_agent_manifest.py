"""Agent manifest → spec 映射的纯逻辑测试：锁死记忆范围回归。

背景：`Agent.from_manifest` 曾直接 `memory=manifest.memory`，role.md 未声明
`memory:` 时主 agent 记忆范围被置为 None，导致 prompt_mgr 跳过项目记忆注入。
现由 `resolve_memory_scope` 兜底：主 agent 缺省 "project"，子 agent 缺省 None。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import Agent, SessionContext
from src.agent.spec import AgentSpec, resolve_memory_scope
from src.mode import RunMode


def test_main_agent_defaults_to_project_memory():
    """主 agent 未声明 memory 时默认加载项目记忆（回归点）。"""
    assert resolve_memory_scope(None, is_subagent=False) == "project"


def test_subagent_defaults_to_no_memory():
    """子 agent 未声明 memory 时默认不加载项目记忆。"""
    assert resolve_memory_scope(None, is_subagent=True) is None


def test_explicit_memory_is_passed_through():
    """显式声明的 memory 无论主/子 agent 均透传。"""
    assert resolve_memory_scope("session", is_subagent=False) == "session"
    assert resolve_memory_scope("project", is_subagent=True) == "project"


def test_spec_from_manifest_without_manifest_still_applies_overrides():
    """manifest 为 None 的回退分支同样应用 overrides，不静默丢弃。"""
    spec = AgentSpec.from_manifest(
        None,
        model="provider/model",
        features={"task"},
        mode=RunMode.PLAN,
    )

    assert spec.agent_type == "agent"
    assert spec.description == ""
    assert spec.memory == "project"
    assert spec.requested_model == "provider/model"
    assert spec.features == frozenset({"task"})
    assert spec.initial_mode is RunMode.PLAN


def test_agent_state_mirrors_spec_and_assembly():
    """Agent 只镜像 spec 与装配产物的初始值，不自行解析模型。"""
    spec = AgentSpec.from_manifest(None, model="fast", mode=RunMode.PLAN)
    assembly = SimpleNamespace(model="openai/resolved")
    deps = SimpleNamespace(session=SessionContext())

    agent = Agent.from_spec(spec, deps, assembly=assembly)

    assert agent.model == "openai/resolved"
    assert agent.mode is RunMode.PLAN
    assert agent.spec is spec
    assert agent.deps is deps
