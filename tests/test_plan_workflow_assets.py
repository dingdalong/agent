"""计划工作流与通用执行提示词回归。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.mgr.role_mgr import RoleMgr
from src.mgr.subagent_mgr import SubAgentMgr
from src.mgr.prompt_mgr import PromptMgr
from src.mgr.task_mgr import TaskManager
from src.mode import RunMode


class _ConfigStub:
    """提供 RoleMgr 所需的最小配置读取接口，缺省角色为 coding。"""

    project_trusted = False
    role_name = "coding"

    def get_config(self, key: str) -> Any:
        if key == "role.default":
            return self.role_name
        raise KeyError(key)

    def get_config_parts(self, parts: tuple[str, ...]) -> Any:
        raise KeyError(parts)


def _role_mgr(tmp_path: Path) -> RoleMgr:
    """构造加载真实内置角色的 RoleMgr（默认 coding）。"""
    return RoleMgr(config_mgr=_ConfigStub(), workdir=tmp_path / "work", global_dir=None)


def test_coding_role_no_longer_exposes_plan_subagent(tmp_path: Path) -> None:
    """coding 的可用子 agent 中不再包含 plan，explore 保留。"""
    role_mgr = _role_mgr(tmp_path)
    deps = SimpleNamespace(role_mgr=role_mgr, llm_mgr=None)

    mgr = SubAgentMgr(tmp_path / "work", deps)

    assert role_mgr.role_name == "coding"
    assert "plan" not in mgr._documents
    assert "explore" in mgr._documents


@pytest.mark.parametrize("role_name", ["coding", "mijia", "onboard", "custom"])
@pytest.mark.parametrize("is_subagent", [False, True])
@pytest.mark.parametrize("can_delegate", [False, True])
def test_execution_guidance_follows_role_and_actual_tools(
    tmp_path: Path, role_name: str, is_subagent: bool, can_delegate: bool,
) -> None:
    """真实角色装配统一规则；仅可委派的主 agent 收到协作指引。"""
    config = _ConfigStub()
    config.role_name = role_name
    global_dir = tmp_path / "global"
    if role_name == "custom":
        role_dir = global_dir / "roles" / "custom"
        role_dir.mkdir(parents=True)
        (role_dir / "role.md").write_text(
            "---\ndescription: 自定义角色\n---\n处理用户给出的领域任务。\n"
        )
    role_mgr = RoleMgr(config_mgr=config, workdir=tmp_path / "work", global_dir=global_dir)
    assert role_mgr.role_name == role_name
    deps = SimpleNamespace(role_mgr=role_mgr, llm_mgr=None)
    subagent_mgr = SubAgentMgr(tmp_path / "work", deps)
    agent = SimpleNamespace(mode=RunMode.EXECUTE,
        deps=deps, is_subagent=is_subagent, memory=None,
        _task_mgr=TaskManager(),
        _subagent_mgr=subagent_mgr if can_delegate else None,
        _skill_mgr=None,
    )
    prompt_mgr = PromptMgr(
        agent=agent, workdir=tmp_path / "work",
        role_prompt="限定子任务" if is_subagent else role_mgr.manifest.prompt,
    )
    text = "\n\n".join(
        message["content"]
        for message in prompt_mgr.build() + prompt_mgr.build_initial_context_messages()
    ) + "\n\n" + prompt_mgr.build_manager_instructions()
    text += "\n\n" + prompt_mgr.build_mode_instructions()
    assert text.count("# 通用执行原则") == 1
    assert text.count("# 工具协议") == 1
    assert text.count("# Manager 工作流") == 1
    assert "# 规划流程" not in text
    assert ("你持续负责理解用户目标" in text) is (not is_subagent)
    assert ("完成委派范围内的任务" in text) is is_subagent
    assert ("# 可用子智能体" in text) is (can_delegate and not is_subagent)
    assert "优先通过 task_delegator" not in text
    system = prompt_mgr.build()[0]["content"]
    manager_instructions = prompt_mgr.build_manager_instructions()
    assert "# Manager 工作流" not in system
    assert ("## 子智能体协作" in manager_instructions) is (
        can_delegate and not is_subagent
    )

    if agent._subagent_mgr is not None:
        subagent_mgr._documents.clear()
    assert "# 可用子智能体" not in "\n\n".join(
        message["content"] for message in prompt_mgr.build_initial_context_messages()
    )
