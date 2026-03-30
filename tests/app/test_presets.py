"""重构后的 presets 测试 — 动态 orchestrator handoff。"""

import pytest

from src.agents.registry import AgentRegistry
from src.app.presets import (
    build_default_graph,
    build_skill_graph,
    _build_handoff_instructions,
)


# ---------------------------------------------------------------------------
# _build_handoff_instructions 单元测试
# ---------------------------------------------------------------------------


class TestBuildHandoffInstructions:
    def test_empty_inputs(self):
        assert _build_handoff_instructions([], None) == ""

    def test_category_summaries_only(self):
        summaries = [
            {"name": "tool_terminal", "description": "终端操作"},
            {"name": "tool_filesystem", "description": "文件操作"},
        ]
        result = _build_handoff_instructions(summaries)
        assert "终端操作相关，交给 tool_terminal" in result
        assert "文件操作相关，交给 tool_filesystem" in result

    def test_with_business_agents(self):
        summaries = [{"name": "tool_terminal", "description": "终端操作"}]
        business = [{"name": "deploy_agent", "description": "部署管理"}]
        result = _build_handoff_instructions(summaries, business)
        assert "终端操作相关，交给 tool_terminal" in result
        assert "部署管理相关，交给 deploy_agent" in result

    def test_business_agents_only(self):
        business = [{"name": "deploy_agent", "description": "部署管理"}]
        result = _build_handoff_instructions([], business)
        assert "部署管理相关，交给 deploy_agent" in result


# ---------------------------------------------------------------------------
# build_default_graph 集成测试
# ---------------------------------------------------------------------------


class TestBuildDefaultGraph:
    def test_with_categories(self):
        registry = AgentRegistry()
        summaries = [
            {"name": "tool_terminal", "description": "终端操作"},
            {"name": "tool_filesystem", "description": "文件操作"},
        ]
        graph = build_default_graph(registry, category_summaries=summaries)
        orchestrator = registry.get("orchestrator")
        assert orchestrator is not None
        assert "tool_terminal" in orchestrator.handoffs
        assert "tool_filesystem" in orchestrator.handoffs
        assert "planner" in orchestrator.handoffs
        assert "终端操作" in orchestrator.instructions
        assert "文件操作" in orchestrator.instructions

    def test_no_categories(self):
        registry = AgentRegistry()
        graph = build_default_graph(registry, category_summaries=[])
        orchestrator = registry.get("orchestrator")
        assert orchestrator is not None
        assert orchestrator.handoffs == ["planner"]

    def test_none_categories(self):
        """category_summaries=None 时等价于空列表。"""
        registry = AgentRegistry()
        graph = build_default_graph(registry)
        orchestrator = registry.get("orchestrator")
        assert orchestrator is not None
        assert "planner" in orchestrator.handoffs

    def test_with_business_agents(self):
        registry = AgentRegistry()
        summaries = [{"name": "tool_terminal", "description": "终端操作"}]
        business = [{"name": "deploy_agent", "description": "部署管理"}]
        graph = build_default_graph(
            registry, category_summaries=summaries, business_agents=business
        )
        orchestrator = registry.get("orchestrator")
        assert "tool_terminal" in orchestrator.handoffs
        assert "deploy_agent" in orchestrator.handoffs
        assert "planner" in orchestrator.handoffs

    def test_planner_registered(self):
        registry = AgentRegistry()
        build_default_graph(registry)
        planner = registry.get("planner")
        assert planner is not None
        assert planner.name == "planner"


# ---------------------------------------------------------------------------
# build_skill_graph 集成测试
# ---------------------------------------------------------------------------


class TestBuildSkillGraph:
    def test_skill_content_in_instructions(self):
        registry = AgentRegistry()
        summaries = [{"name": "tool_terminal", "description": "终端操作"}]
        graph = build_skill_graph(
            registry, skill_content="你是一个技能", category_summaries=summaries
        )
        orchestrator = registry.get("orchestrator")
        assert "你是一个技能" in orchestrator.instructions
        assert "tool_terminal" in orchestrator.handoffs

    def test_skill_without_categories(self):
        registry = AgentRegistry()
        graph = build_skill_graph(registry, skill_content="你是一个技能")
        orchestrator = registry.get("orchestrator")
        assert "你是一个技能" in orchestrator.instructions
        assert orchestrator.handoffs == ["planner"]


# ---------------------------------------------------------------------------
# 占位 agent 已移除
# ---------------------------------------------------------------------------


class TestNoSpecialistAgents:
    """验证占位 agent 已被移除。"""

    def test_no_specialist_agents_registered(self):
        registry = AgentRegistry()
        build_default_graph(registry)
        assert registry.get("weather_agent") is None
        assert registry.get("calendar_agent") is None
        assert registry.get("email_agent") is None
