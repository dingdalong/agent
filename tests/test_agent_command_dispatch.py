"""Agent._on_request_input 经 CommandMgr 分发斜杠命令的集成测试。"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext
from src.commands.mgr import CommandMgr


class RecordingEventBus:
    def __init__(self, inputs: list[str]) -> None:
        self.inputs = iter(inputs)
        self.outputs: list[str] = []
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)

    async def request_input(self, prompt: str, default: str = "") -> str:
        return next(self.inputs)

    async def request_output(self, content: str, **kwargs: object) -> None:
        self.outputs.append(content)


def _agent(inputs: list[str], *, features: set[str] | None, tmp_path: Path) -> Agent:
    """构造仅携带命令分发所需依赖的最小 Agent + 真 CommandMgr。"""
    bus = RecordingEventBus(inputs)
    command_mgr = CommandMgr(workdir=tmp_path, global_dir=None, project_trusted=False)
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.features = features
    agent.deps = SimpleNamespace(event_bus=bus, command_mgr=command_mgr, hooks_mgr=None)
    agent.set_plan_active = lambda active: True
    return agent


def test_plan_command_handled_in_agent(tmp_path: Path) -> None:
    """agent 层命令 /plan：输出提示、返回 REQUEST_INPUT、不挂 ctx.command。"""
    agent = _agent(["/plan"], features={"plan"}, tmp_path=tmp_path)
    ctx = RunContext(messages=[])
    state = asyncio.run(agent._on_request_input(ctx))
    assert state is AgentState.REQUEST_INPUT
    assert ctx.command is None
    assert agent.deps.event_bus.outputs == ["已进入计划模式。\n"]


def test_plan_command_feature_gated(tmp_path: Path) -> None:
    """plan feature 关闭时 /plan 不可用。"""
    agent = _agent(["/plan"], features={"file"}, tmp_path=tmp_path)
    ctx = RunContext(messages=[])
    state = asyncio.run(agent._on_request_input(ctx))
    assert state is AgentState.REQUEST_INPUT
    assert "不可用" in agent.deps.event_bus.outputs[0]


def test_clear_command_deferred_to_app(tmp_path: Path) -> None:
    """app 层命令 /clear：返回 DONE 并挂 ctx.command，agent 内不执行。"""
    agent = _agent(["/clear"], features=None, tmp_path=tmp_path)
    ctx = RunContext(messages=[])
    state = asyncio.run(agent._on_request_input(ctx))
    assert state is AgentState.DONE
    assert ctx.command == ("clear", [])


def test_unknown_command(tmp_path: Path) -> None:
    """未知命令：输出提示并返回 REQUEST_INPUT。"""
    agent = _agent(["/nosuch"], features=None, tmp_path=tmp_path)
    ctx = RunContext(messages=[])
    state = asyncio.run(agent._on_request_input(ctx))
    assert state is AgentState.REQUEST_INPUT
    assert ctx.command is None
    assert agent.deps.event_bus.outputs == ["未知命令: /nosuch\n"]
