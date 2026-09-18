"""Agent 层与 ReminderMgr 的装配点测试。

`.agent/memory/提醒注入链路-remindermgr-planmgr-的约束与测试盲区.md` 点名的缺口：
`tests/test_agent_pause_turn.py` 与 `tests/test_agent_llm_failure.py` 中的
`NoopReminder` 是**整体替换** `agent._reminder_mgr` 的，因此所有 agent 用例都不走
真实 ReminderMgr——若 `agent.py` 里把 `self.is_subagent` 写死成常量，全量测试仍会
全绿，只在真实会话的首个 turn 才暴露。

本文件用一个记录实参的假 provider 补上这个缺口：它不关心 ReminderMgr 内部实现，
只断言 Agent 传下去的实参确实取自 `self.is_subagent` 与 `self.mode`。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.agent import Agent
from src.agent.states import RunContext, RunResult
from src.mgr.reminder_mgr import ReminderMgr
from src.mode import RunMode


class _RecordingReminder:
    """记录 queue_turn_start 收到的实参。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[tuple[object, object]] = []
        self.pending: list[str] = []

    def queue_turn_start(self, mode: object, is_subagent: object) -> None:
        """记录实参。

        Args:
            mode: 调用方 agent 的运行模式。
            is_subagent: 调用方是否为子智能体。

        """
        self.calls.append((mode, is_subagent))

    def pop_pending(self) -> list[str]:
        pending = self.pending
        self.pending = []
        return pending


def _agent(*, is_subagent: bool, mode: RunMode) -> tuple[Agent, _RecordingReminder]:
    """装配一个只跑到 `_run_single_turn` 的最小 Agent。

    Args:
        is_subagent: 目标 agent 的子智能体标志。
        mode: 目标 agent 的运行模式。

    Returns:
        (Agent 实例, 记录用的 reminder)。
    """
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "worker" if is_subagent else "main"
    agent.description = ""
    agent.history = []
    agent.is_subagent = is_subagent
    agent.mode = mode
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
@pytest.mark.parametrize("mode", [RunMode.PLAN, RunMode.EXECUTE], ids=["plan", "execute"])
def test_run_passes_own_identity_to_reminder(is_subagent: bool, mode: RunMode) -> None:
    """单轮路径传给 ReminderMgr 的实参取自本 agent，不是写死的常量。

    Args:
        is_subagent: 目标 agent 的子智能体标志。
        mode: 目标 agent 的运行模式。

    Returns:
        None。
    """
    agent, reminder = _agent(is_subagent=is_subagent, mode=mode)

    asyncio.run(agent.run("任务正文"))

    assert reminder.calls == [(mode, is_subagent)]


def test_turn_start_instructions_remain_separate_from_task() -> None:
    """框架提醒保持独立排队，不拼接或改写用户任务。

    Returns:
        None。
    """
    agent, reminder = _agent(is_subagent=True, mode=RunMode.PLAN)
    reminder.pending.append("只读")

    asyncio.run(agent.run("任务正文"))

    assert [message["role"] for message in agent.history] == ["developer", "user"]
    assert "只读" in agent.history[0]["content"]
    assert agent.history[1] == {"role": "user", "content": "任务正文"}
    assert reminder.pop_pending() == []


def test_first_framework_message_precedes_real_user_request() -> None:
    """Manager 工作流、模式和提醒在首个真实 user 之前合并。"""
    agent, reminder = _agent(is_subagent=False, mode=RunMode.PLAN)
    agent._prompt_mgr = SimpleNamespace(
        build_initial_context_messages=lambda: [{
            "role": "user",
            "content": "CATALOGS\nAGENTS\nENVIRONMENT",
        }],
        build_capability_instructions=lambda: "CAPABILITY_BOUNDARY",
        build_manager_instructions=lambda: "MANAGER_WORKFLOW",
        build_mode_instructions=lambda: "MODE_PLAN",
    )
    reminder.pending.append("TURN_REMINDER")

    asyncio.run(agent.run("REAL_USER_REQUEST"))

    assert [message["role"] for message in agent.history] == [
        "user", "developer", "user",
    ]
    assert agent.history[0]["content"] == "CATALOGS\nAGENTS\nENVIRONMENT"
    developer = agent.history[1]["content"]
    positions = [
        developer.index(marker)
        for marker in (
            "CAPABILITY_BOUNDARY",
            "MANAGER_WORKFLOW",
            "MODE_PLAN",
            "TURN_REMINDER",
        )
    ]
    assert positions == sorted(positions)
    assert agent.history[2]["content"] == "REAL_USER_REQUEST"
    assert agent._initial_context_message_count == 1


def test_chat_boundary_combines_mode_and_reminders_into_one_developer() -> None:
    """一次 chat 前的模式与多项框架提醒合并成一条 developer 消息。"""
    agent = object.__new__(Agent)
    agent.history = [{"role": "user", "content": "任务正文"}]
    agent.mode = RunMode.EXECUTE
    agent.is_subagent = False
    agent.deps = SimpleNamespace(data_guard=None, session_state=None)
    agent._last_injected_mode = None
    agent._prompt_mgr = SimpleNamespace(
        build_mode_instructions=lambda: f"MODE-{agent.mode.value}",
    )
    agent._reminder_mgr = ReminderMgr()
    ctx = RunContext(
        messages=agent.history,
        pending_framework_instructions=["提醒 A", "提醒 A", "提醒 B"],
    )

    agent._append_pending_framework_message(ctx)

    assert [message["role"] for message in agent.history] == ["user", "developer"]
    content = agent.history[-1]["content"]
    assert "MODE-execute" in content
    assert content.count("提醒 A") == 1
    assert content.count("提醒 B") == 1
    assert ctx.pending_framework_instructions == []

    agent._append_pending_framework_message(ctx)
    assert len(agent.history) == 2


def test_capability_guidance_refreshes_after_mode_change() -> None:
    """能力摘要在模式切换后的下一个 chat 边界重新追加。"""
    agent = object.__new__(Agent)
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent.is_subagent = True
    agent.deps = SimpleNamespace(data_guard=None, session_state=None)
    agent._prompt_mgr = SimpleNamespace(
        build_capability_instructions=lambda: f"CAPABILITY-{agent.mode.value}",
        build_mode_instructions=lambda: f"MODE-{agent.mode.value}",
    )
    agent._reminder_mgr = ReminderMgr()

    ctx = RunContext(messages=agent.history)
    agent._append_pending_framework_message(ctx)
    assert "CAPABILITY-execute" in agent.history[-1]["content"]

    agent.mode = RunMode.PLAN
    ctx = RunContext(messages=agent.history)
    agent._append_pending_framework_message(ctx)
    assert "CAPABILITY-plan" in agent.history[-1]["content"]


def test_mode_injection_uses_final_mode_at_next_chat_boundary() -> None:
    """多次模式切换不即时写历史，下一次 chat 只注入最终模式。"""
    agent = object.__new__(Agent)
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent.is_subagent = False
    agent.deps = SimpleNamespace(data_guard=None, session_state=None)
    agent._last_injected_mode = RunMode.EXECUTE
    agent._prompt_mgr = SimpleNamespace(
        build_mode_instructions=lambda: f"MODE-{agent.mode.value}",
    )
    agent._reminder_mgr = ReminderMgr()
    ctx = RunContext(messages=agent.history)

    agent.mode = RunMode.PLAN
    agent.mode = RunMode.EXECUTE
    agent._append_pending_framework_message(ctx)
    assert agent.history == []

    agent.mode = RunMode.PLAN
    agent._append_pending_framework_message(ctx)
    assert len(agent.history) == 1
    assert agent.history[0]["role"] == "developer"
    assert "MODE-plan" in agent.history[0]["content"]
