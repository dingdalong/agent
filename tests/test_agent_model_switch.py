from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.agent.agent import Agent
from src.mgr.config_mgr import ConfigManager


class ConfigStub:
    def get_config(self, key: str):
        assert key == "compact"
        return {
            "auto_compact_rate": 0.8,
            "keep_recent_user_turns": 3,
            "keep_recent_messages_token_rate": 0.25,
        }


def test_switch_model_rebinds_model_state_without_replacing_history(tmp_path: Path) -> None:
    old_llm = SimpleNamespace(model="old-model", reasoning_effort="high", context_limit=1000)
    new_llm = SimpleNamespace(model="new-model", reasoning_effort="max", context_limit=2000)
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.model = "old-model"
    agent.reasoning_effort = "high"
    agent.llm = old_llm
    agent.history = [{"role": "user", "content": "保留历史"}]
    agent._input_history = ["保留输入"]
    invalidations = []
    agent._prompt_mgr = SimpleNamespace(
        model="old-model",
        invalidate_cache=lambda: invalidations.append(True),
    )
    agent.deps = SimpleNamespace(
        llm_mgr=SimpleNamespace(get=lambda model: new_llm if model == "new-model" else old_llm),
        config_mgr=ConfigStub(),
        workdir=tmp_path,
        data_guard=None,
    )
    history = agent.history
    input_history = agent._input_history
    agent_uuid = agent.uuid
    agent._compact_mgr = agent._build_compact_mgr(old_llm)
    agent._compact_mgr.recent_files = ["src/old.py"]
    agent._compact_mgr.has_compacted = True

    agent.switch_model("new-model", "xhigh")

    assert agent.uuid == agent_uuid
    assert agent.history is history
    assert agent._input_history is input_history
    assert agent.history == [{"role": "user", "content": "保留历史"}]
    assert agent.llm is new_llm
    assert agent.model == "new-model"
    assert agent.reasoning_effort == "xhigh"
    assert agent._compact_mgr.llm is new_llm
    assert agent._compact_mgr.auto_compact_size == 1600
    assert agent._compact_mgr.recent_messages_token_limit == 500
    assert agent._compact_mgr.recent_files == ["src/old.py"]
    assert agent._compact_mgr.has_compacted is True
    assert agent._prompt_mgr.model == "new-model"
    assert invalidations == [True]


def test_model_role_overrides_are_written_together(tmp_path: Path) -> None:
    manager = ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=True,
    )

    manager.set_configs(
        {
            "role.coding.model": {"default": "new-model", "fast": "fast-model"},
            "role.coding.reasoning_effort": "xhigh",
        },
        "project",
    )

    assert yaml.safe_load(manager.project_config_path.read_text()) == {
        "role": {
            "coding": {
                "model": {"default": "new-model", "fast": "fast-model"},
                "reasoning_effort": "xhigh",
            }
        }
    }


def test_model_slots_mapping_overwrites_legacy_scalar_model(tmp_path: Path) -> None:
    """项目层残留旧标量 model 时，整体写父键 mapping 应成功抹平旧格式。"""
    manager = ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=True,
    )
    manager.project_config_path.parent.mkdir(parents=True)
    manager.project_config_path.write_text(
        "role:\n  coding:\n    model: old-model\n    reasoning_effort: high\n"
    )

    manager.set_configs(
        {
            "role.coding.model": {"default": "new-model", "fast": "fast-model"},
            "role.coding.reasoning_effort": "max",
        },
        "project",
    )

    assert yaml.safe_load(manager.project_config_path.read_text()) == {
        "role": {
            "coding": {
                "model": {"default": "new-model", "fast": "fast-model"},
                "reasoning_effort": "max",
            }
        }
    }


def test_model_role_override_failure_preserves_project_config(tmp_path: Path) -> None:
    manager = ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=True,
    )
    manager.project_config_path.parent.mkdir(parents=True)
    original = "role:\n  coding: scalar\n"
    manager.project_config_path.write_text(original)

    with pytest.raises(ValueError):
        manager.set_configs(
            {
                "role.coding.model": "new-model",
                "role.coding.reasoning_effort": "xhigh",
            },
            "project",
        )

    assert manager.project_config_path.read_text() == original
