"""Tests for agent definitions and graph building."""
from unittest.mock import AsyncMock
from src.agents import AgentRegistry
from src.agents.definitions import build_default_graph, build_skill_graph


class TestBuildDefaultGraph:

    def test_registers_all_agents(self):
        registry = AgentRegistry()
        build_default_graph(registry)
        expected = {"weather_agent", "calendar_agent", "email_agent", "orchestrator", "planner"}
        actual = {a.name for a in registry.all_agents()}
        assert actual == expected

    def test_returns_compiled_graph(self):
        registry = AgentRegistry()
        graph = build_default_graph(registry)
        assert graph is not None
        assert hasattr(graph, "entry")

    def test_entry_is_orchestrator(self):
        registry = AgentRegistry()
        graph = build_default_graph(registry)
        assert graph.entry == "orchestrator"


class TestBuildSkillGraph:

    def test_injects_skill_content_into_orchestrator(self):
        registry = AgentRegistry()
        build_skill_graph(registry, "你是一个代码助手。")
        orchestrator = registry.get("orchestrator")
        assert "你是一个代码助手。" in orchestrator.instructions

    def test_returns_compiled_graph(self):
        registry = AgentRegistry()
        graph = build_skill_graph(registry, "skill content")
        assert graph is not None
        assert graph.entry == "orchestrator"
