"""Agent 层与 ReminderMgr 的装配点测试。

`.agent/memory/提醒注入链路-remindermgr-planmgr-的约束与测试盲区.md` 点名的缺口：
`tests/test_agent_pause_turn.py` 与 `tests/test_agent_llm_failure.py` 中的
`NoopReminder` 是**整体替换** `agent._reminder_mgr` 的，因此所有 agent 用例都不走
真实 ReminderMgr——若 `agent.py` 里把 `self.is_subagent` 写死成常量，全量测试仍会
全绿，只在真实会话的首个 turn 才暴露。

本文件用一个记录实参的假 provider 补上这个缺口：它不关心 ReminderMgr 内部实现，
只断言 Agent 传下去的实参确实取自 `self.is_subagent` 与 `self.plan_active`。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.agent import Agent
from src.agent.states import RunResult


class _RecordingReminder:
    """记录 build_turn_start_instructions 收到的实参。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[tuple[object, object]] = []

    def build_turn_start_instructions(self, plan_active: object, is_subagent: object) -> str:
        """记录实参并返回空注入。

        Args:
            plan_active: 调用方 agent 的 Plan 状态。
            is_subagent: 调用方是否为子智能体。

        Returns:
            空字符串（本用例只关心实参）。
        """
        self.calls.append((plan_active, is_subagent))
        return ""


def _agent(*, is_subagent: bool, plan_active: bool) -> tuple[Agent, _RecordingReminder]:
    """装配一个只跑到 `_run_single_turn` 的最小 Agent。

    Args:
        is_subagent: 目标 agent 的子智能体标志。
        plan_active: 目标 agent 的 Plan 状态。

    Returns:
        (Agent 实例, 记录用的 reminder)。
    """
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "worker" if is_subagent else "main"
    agent.description = ""
    agent.history = []
    agent.is_subagent = is_subagent
    agent.plan_active = plan_active
    agent.deps = SimpleNamespace(data_guard=None, session_state=None)
    agent.llm = SimpleNamespace(clear_reasoning_content=lambda _messages: None)
    reminder = _RecordingReminder()
    agent._reminder_mgr = reminder

    async def _single_turn(ctx: object, state: object) -> RunResult:
        """短路状态机，直接返回空结果。

        Args:
            ctx: 运行上下文。
            state: 起始状态。

        Returns:
            空运行结果。
        """
        del ctx, state
        return RunResult(final_text="done")

    async def _cleanup(ctx: object) -> None:
        """跳过轮末任务清理。

        Args:
            ctx: 运行上下文。

        Returns:
            None。
        """
        del ctx

    agent._run_single_turn = _single_turn
    agent._cleanup_tasks_at_turn_end = _cleanup
    return agent, reminder


@pytest.mark.parametrize("is_subagent", [True, False], ids=["subagent", "main"])
@pytest.mark.parametrize("plan_active", [True, False], ids=["plan-on", "plan-off"])
def test_run_passes_own_identity_to_reminder(is_subagent: bool, plan_active: bool) -> None:
    """单轮路径传给 ReminderMgr 的实参取自本 agent，不是写死的常量。

    Args:
        is_subagent: 目标 agent 的子智能体标志。
        plan_active: 目标 agent 的 Plan 状态。

    Returns:
        None。
    """
    agent, reminder = _agent(is_subagent=is_subagent, plan_active=plan_active)

    asyncio.run(agent.run("任务正文"))

    assert reminder.calls == [(plan_active, is_subagent)]


def test_turn_start_instructions_are_prepended_before_task() -> None:
    """非空注入拼在任务正文之前，且原任务正文完整保留。

    Returns:
        None。
    """
    agent, reminder = _agent(is_subagent=True, plan_active=True)
    reminder.build_turn_start_instructions = lambda *_: "<reminder>只读</reminder>"

    asyncio.run(agent.run("任务正文"))

    content = agent.history[0]["content"]
    assert content.startswith("<reminder>只读</reminder>")
    assert content.endswith("任务正文")
