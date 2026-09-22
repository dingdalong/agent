from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.agent.agent import Agent
from src.agent.assembly import AgentAssembly, AgentAssembler
from src.agent.spec import AgentSpec
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
    old_llm = SimpleNamespace(provider_name="openai", model="old-model", reasoning_effort="high", context_limit=1000)
    new_llm = SimpleNamespace(provider_name="anthropic", model="new-model", reasoning_effort="max", context_limit=2000)
    deps = SimpleNamespace(
        llm_mgr=SimpleNamespace(get=lambda model: new_llm if model == "new-model" else old_llm),
        config_mgr=ConfigStub(),
        workdir=tmp_path,
        data_guard=None,
        session=SimpleNamespace(state=SimpleNamespace(input_history=lambda: input_history)),
    )
    spec = AgentSpec(agent_type="main", description="")
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.spec = spec
    agent.model = "old-model"
    agent.reasoning_effort = "high"
    agent.history = [{"role": "user", "content": "保留历史"}]
    input_history = ["保留输入"]
    agent.deps = deps
    history = agent.history
    agent_uuid = agent.uuid
    agent.assembly = AgentAssembly(
        llm=old_llm,
        model="openai/old-model",
        compact_mgr=AgentAssembler(deps).build_compact_mgr(
            spec, old_llm, agent_uuid=agent_uuid,
        ),
        prompt_mgr=SimpleNamespace(),
        reminder_mgr=None,
        task_mgr=None,
        skill_mgr=None,
        subagent_mgr=None,
        system_prompt=[],
    )
    agent.assembly.compact_mgr.has_compacted = True

    agent.switch_model("new-model", "xhigh")

    assert agent.uuid == agent_uuid
    assert agent.history is history
    assert agent.get_input_history() is input_history
    assert agent.history == [{"role": "user", "content": "保留历史"}]
    assert agent.llm is new_llm
    assert agent.model == "anthropic/new-model"
    assert agent.reasoning_effort == "xhigh"
    assert agent.assembly.llm is new_llm
    assert agent.assembly.compact_mgr.llm is new_llm
    assert agent.assembly.compact_mgr.auto_compact_size == 1600
    assert agent.assembly.compact_mgr.recent_messages_token_limit == 500
    assert agent.assembly.compact_mgr.has_compacted is True


def test_model_role_overrides_are_written_together(tmp_path: Path) -> None:
    manager = ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=True,
    )

    manager.set_configs_parts(
        {
            ("role", "coding", "model"): {"default": "new-model", "fast": "fast-model"},
            ("role", "coding", "reasoning_effort"): "xhigh",
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

    manager.set_configs_parts(
        {
            ("role", "coding", "model"): {"default": "new-model", "fast": "fast-model"},
            ("role", "coding", "reasoning_effort"): "max",
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
        manager.set_configs_parts(
            {
                ("role", "coding", "model"): "new-model",
                ("role", "coding", "reasoning_effort"): "xhigh",
            },
            "project",
        )

    assert manager.project_config_path.read_text() == original


def test_agent_declares_assembly_as_dataclass_field() -> None:
    """装配产物是声明的 dataclass 字段；provider 不再挂在 Agent 自身。"""
    names = {item.name for item in dataclasses.fields(Agent)}
    assert "assembly" in names
    assert "llm" not in names
