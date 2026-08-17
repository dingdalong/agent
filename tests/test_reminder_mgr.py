"""ReminderMgr 提醒源调用链路测试 — 锁住 (plan_active, is_subagent) 按位置透传给提醒源。"""

from __future__ import annotations

from pathlib import Path

from src.mgr.plan_mgr import _PLAN_SKILL_KEY, PlanMgr
from src.mgr.reminder_mgr import ReminderMgr
from src.mgr.task_mgr import TaskManager


class _RecordingProvider:
    """记录 ReminderMgr 传入实参的假提醒源。

    两个接口方法的参数均声明为仅位置（/），中介若改用关键字传参会直接抛 TypeError。

    Attributes:
        turn_start_calls: get_turn_start_reminder 收到的 (plan_active, is_subagent) 实参序列。
        post_round_calls: pop_post_round_reminder 收到的 (plan_active, is_subagent) 实参序列。
    """

    def __init__(self) -> None:
        self.turn_start_calls: list[tuple[bool, bool]] = []
        self.post_round_calls: list[tuple[bool, bool]] = []

    def get_turn_start_reminder(self, plan_active: bool, is_subagent: bool, /) -> str:
        """记录 turn start 实参并返回带实参取值的可识别文本。

        Args:
            plan_active: 中介透传的计划模式标志。
            is_subagent: 中介透传的子智能体标志。

        Returns:
            形如 "TURN-True-False" 的可识别文本。
        """
        self.turn_start_calls.append((plan_active, is_subagent))
        return f"TURN-{plan_active}-{is_subagent}"

    def pop_post_round_reminder(self, plan_active: bool, is_subagent: bool, /) -> str | None:
        """记录 post round 实参并返回带实参取值的可识别文本。

        Args:
            plan_active: 中介透传的计划模式标志。
            is_subagent: 中介透传的子智能体标志。

        Returns:
            形如 "POST-True-False" 的可识别文本。
        """
        self.post_round_calls.append((plan_active, is_subagent))
        return f"POST-{plan_active}-{is_subagent}"


def test_turn_start_forwards_both_args_positionally() -> None:
    """build_turn_start_instructions 按位置原样透传两个实参，返回值用 <reminder> 包装。"""
    mgr = ReminderMgr()
    provider = _RecordingProvider()
    mgr.register(provider)

    sub_text = mgr.build_turn_start_instructions(True, True)
    main_text = mgr.build_turn_start_instructions(False, False)

    assert provider.turn_start_calls == [(True, True), (False, False)]
    assert sub_text == "<reminder>TURN-True-True</reminder>"
    assert main_text == "<reminder>TURN-False-False</reminder>"


def test_post_round_forwards_both_args_positionally() -> None:
    """collect_post_round_messages 按位置原样透传两个实参，构造 <reminder> 包装的 user 消息。"""
    mgr = ReminderMgr()
    provider = _RecordingProvider()
    mgr.register(provider)

    sub_msgs = mgr.collect_post_round_messages(True, True)
    main_msgs = mgr.collect_post_round_messages(False, False)

    assert provider.post_round_calls == [(True, True), (False, False)]
    assert sub_msgs == [
        {"role": "user", "content": "<reminder>POST-True-True</reminder>"},
    ]
    assert main_msgs == [
        {"role": "user", "content": "<reminder>POST-False-False</reminder>"},
    ]


def test_real_providers_accept_new_signature(tmp_path: Path) -> None:
    """真 PlanMgr 与真 TaskManager 注册到同一中介后，两个收集方法都能以两个位置参数调用。"""
    mgr = ReminderMgr()
    plan_mgr = PlanMgr(tmp_path)
    task_mgr = TaskManager()
    mgr.register(plan_mgr)
    mgr.register(task_mgr)
    # 造出 TaskManager 真正产出提醒的条件：有未完成任务且连续 3 轮未调用任务工具
    task_mgr.create("task 1", "desc 1")
    for _ in range(3):
        task_mgr.notify_tool_round(["read_file"])

    turn_start = mgr.build_turn_start_instructions(True, False)
    post_round = mgr.collect_post_round_messages(True, False)

    assert _PLAN_SKILL_KEY in turn_start
    assert "当前任务列表" in turn_start
    assert post_round == [
        {"role": "user", "content": "<reminder>更新你的任务列表。</reminder>"},
    ]


def test_identity_branch_through_reminder_mgr(tmp_path: Path) -> None:
    """经中介调用真 PlanMgr 时身份一路传到指令生成：主 agent 拿主版、子 agent 拿子版。"""
    mgr = ReminderMgr()
    mgr.register(PlanMgr(tmp_path))

    main_text = mgr.build_turn_start_instructions(True, False)
    sub_text = mgr.build_turn_start_instructions(True, True)

    assert main_text.startswith("<reminder>")
    assert _PLAN_SKILL_KEY in main_text
    assert _PLAN_SKILL_KEY not in sub_text
    assert "## 产出" in sub_text
    assert main_text != sub_text
