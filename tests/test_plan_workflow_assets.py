"""plan 工作流资产回归：coding 不再暴露 plan 子 agent，plan-workflow 技能仍可加载。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.mgr.role_mgr import RoleMgr
from src.mgr.skill_mgr import SkillMgr
from src.mgr.subagent_mgr import SubAgentMgr


class _ConfigStub:
    """提供 RoleMgr 所需的最小配置读取接口，缺省角色为 coding。"""

    project_trusted = False

    def get_config(self, key: str) -> Any:
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
    assert "Plan 模式 vs task_* 工具" in text
    assert "enter_plan_mode" not in text
    # explore 委派只给拆分原则，不设固定数量上限，也不引用运行中不可知的框架参数
    assert "最多 3 个" not in text
    assert "llm.concurrency" not in text
    assert "每个独立探索方向" in text or "方向重叠就合并" in text
