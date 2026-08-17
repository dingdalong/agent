"""PlanMgr 计划模式指令的主/子 agent 分叉测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.mgr.plan_mgr import _PLAN_SKILL_KEY, PlanMgr
from src.mgr.reminder_mgr import ReminderMgr


def _enter_plan(mgr: PlanMgr) -> None:
    """走真实入口置上 _pending_injection。

    Args:
        mgr: 待进入计划模式的 PlanMgr 实例。
    """
    mgr.enter_mode(SimpleNamespace(plan_active=False), ReminderMgr())


def test_turn_start_main_agent_keeps_plan_file_guidance(tmp_path: Path) -> None:
    """主 agent 的 turn start 指令保留计划技能、计划目录与计划文件写入引导。"""
    mgr = PlanMgr(tmp_path)

    text = mgr.get_turn_start_reminder(True, False)

    assert _PLAN_SKILL_KEY in text
    assert str(tmp_path / ".agent" / "plans") in text
    assert "write_file / edit_file_lines" in text
    assert "禁止编辑、创建或删除计划文件以外的任何项目文件" in text
    assert "你在为规划阶段收集信息" not in text


def test_turn_start_subagent_drops_plan_file_guidance(tmp_path: Path) -> None:
    """子 agent 的 turn start 指令去掉计划文件写入引导，只保留只读探索与产出要求。"""
    mgr = PlanMgr(tmp_path)

    text = mgr.get_turn_start_reminder(True, True)

    assert _PLAN_SKILL_KEY not in text
    assert str(tmp_path / ".agent" / "plans") not in text
    assert "write_file" not in text
    assert "edit_file_lines" not in text
    assert "你在为规划阶段收集信息" in text
    assert "禁止编辑、创建或删除任何项目文件" in text
    assert "## 产出" in text
    assert "不要尝试写入任何文件" in text


def test_subagent_omits_active_plan_section(tmp_path: Path) -> None:
    """活跃计划路径只出现在主 agent 指令中，子 agent 指令不含「## 当前计划」段。"""
    mgr = PlanMgr(tmp_path)
    mgr.set_active_plan_path("/x/plan.md")

    main_text = mgr.get_turn_start_reminder(True, False)
    sub_text = mgr.get_turn_start_reminder(True, True)

    assert "## 当前计划" in main_text
    assert "/x/plan.md" in main_text
    assert "## 当前计划" not in sub_text
    assert "/x/plan.md" not in sub_text


def test_post_round_main_agent_returns_main_instructions(tmp_path: Path) -> None:
    """主 agent 轮中进入计划模式后拿到主版指令，且 _pending_injection 只消费一次。"""
    mgr = PlanMgr(tmp_path)
    _enter_plan(mgr)

    first = mgr.pop_post_round_reminder(True, False)

    assert first is not None
    assert _PLAN_SKILL_KEY in first
    assert mgr.pop_post_round_reminder(True, False) is None


def test_post_round_subagent_returns_subagent_instructions(tmp_path: Path) -> None:
    """子 agent 轮中进入计划模式后拿到子版指令，不含计划技能引导。"""
    mgr = PlanMgr(tmp_path)
    _enter_plan(mgr)

    text = mgr.pop_post_round_reminder(True, True)

    assert text is not None
    assert _PLAN_SKILL_KEY not in text
    assert "## 产出" in text


def test_same_instance_serves_both_identities(tmp_path: Path) -> None:
    """单例被主/子 agent 交替调用时按参数分叉，身份不会被缓存进实例字段。"""
    mgr = PlanMgr(tmp_path)

    main_first = mgr.get_turn_start_reminder(True, False)
    sub = mgr.get_turn_start_reminder(True, True)
    main_again = mgr.get_turn_start_reminder(True, False)

    assert main_first != sub
    assert main_again == main_first
