"""斜杠命令执行上下文与结果。

CommandContext 是所有命令 handler 的统一入参，聚合三层能力：
进程级依赖（deps）、当前前台 Agent（agent）、应用层门面（app）。

分层约定：
- agent 层命令（plan/models/resume/help 等）在 Agent._on_request_input 内分发，app=None。
- app 层命令（clear/agents 等需要主循环上下文的）不在 agent 内执行，
  由 CommandMgr defer 后经 RunResult.command 上抛，AgentApp.run 二次 dispatch 时
  传入 app=AgentApp 自身（结构式满足 CommandAppServices）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps
    from src.interfaces.agent_view_store import AgentViewStore


class CommandAppServices(Protocol):
    """app 层命令所需的最小门面。

    AgentApp 结构式满足（鸭子类型，无需显式继承，避免 import 环）。
    """

    deps: "AgentDeps"
    agent_view_store: "AgentViewStore"

    async def reset_session(self, *, source: str = "clear") -> "Agent":
        """重置会话并返回新的前台 Agent。"""
        ...


@dataclass
class CommandContext:
    """所有命令 handler 的统一入参。

    Attributes:
        deps: 进程级依赖集合（各 Manager、EventBus、UI 等）。
        agent: 当前前台 Agent；agent 层分发恒有，app 层分发为旧 agent。
        app: 应用层门面；仅 app 层分发（defer 后）非 None。
    """

    deps: "AgentDeps"
    agent: "Agent | None" = None
    app: CommandAppServices | None = None


@dataclass
class CommandResult:
    """handler 可选返回值。

    Attributes:
        new_agent: 仅 app 层 handler 用——请求主循环替换前台 Agent（/clear 用）。
    """

    new_agent: "Agent | None" = None
