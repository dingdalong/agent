"""plan 工作流资产回归：coding 不再暴露 plan 子 agent，plan-workflow 技能仍可加载。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.mgr.role_mgr import RoleMgr
from src.mgr.skill_mgr import SkillMgr
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


def test_coding_role_plan_workflow_skill_still_loads(tmp_path: Path) -> None:
    """coding 仍可加载重写后的 builtin:plan-workflow 技能。"""
    role_mgr = _role_mgr(tmp_path)
    mgr = SkillMgr(workdir=tmp_path / "work", role_mgr=role_mgr)

    assert mgr.check_skill("builtin:plan-workflow")
    text = mgr.load_full_text("builtin:plan-workflow")
    assert "不要创建执行进度任务" in text
    assert "计划模式只调查与规划" not in text
    assert "enter_plan_mode" not in text
    assert "最多 3 个" not in text
    assert "llm.concurrency" not in text
    assert "首次探索将独立的文件发现、内容搜索和读取合并到同一轮并行调用" in text
    assert "builtin:plan-workflow" not in mgr.describe()


def test_coding_execute_plan_skill_loads(tmp_path: Path) -> None:
    """批准后执行使用真实角色技能，支持主 agent 连续推进。"""
    mgr = SkillMgr(workdir=tmp_path / "work", role_mgr=_role_mgr(tmp_path))

    assert mgr.check_skill("builtin:execute-plan")
    text = mgr.load_full_text("builtin:execute-plan")
    assert "接续计划" in text
    assert "推进实现" in text
    assert "验证与交付" in text
    assert "builtin:execute-plan" not in mgr.describe()


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
    ) + "\n\n" + prompt_mgr.build_mode_instructions()
    assert text.count("# 执行原则") == 1
    assert text.count("# 工具选择与恢复") == 1
    assert "决定提交后不再" not in text  # 规划流程只由按需加载的技能注入。
    assert ("你持续负责理解用户目标" in text) is (not is_subagent)
    assert ("完成委派范围内的任务" in text) is is_subagent
    assert ("# 可用子智能体" in text) is (can_delegate and not is_subagent)
    assert "优先通过 task_delegator" not in text

    if agent._subagent_mgr is not None:
        subagent_mgr._documents.clear()
    assert "# 可用子智能体" not in "\n\n".join(
        message["content"] for message in prompt_mgr.build_initial_context_messages()
    )
