"""AgentDeps 字段完整性测试。"""
from unittest.mock import MagicMock

from src.agents.deps import AgentDeps


def test_agent_deps_has_runner_field():
    """AgentDeps 应包含 runner 字段。"""
    mock_runner = MagicMock()
    deps = AgentDeps(runner=mock_runner)
    assert deps.runner is mock_runner


def test_agent_deps_runner_defaults_to_none():
    """runner 字段默认为 None。"""
    deps = AgentDeps()
    assert deps.runner is None
