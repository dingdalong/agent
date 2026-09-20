"""Manager 层感知调用方所需的最小事实面。

`ToolsMgr` 只需要这些事实来做授权判定、hook 上下文与用户意图提取，
不需要 `Agent` 的状态机、装配产物或 Manager。`Agent` 通过只读属性满足
本协议，测试也可以用最小替身满足它——两侧都不必互相 import。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.mode import RunMode


class ModeOwner(Protocol):
    """可被计划模式切换改写的调用方。

    `PlanMgr` 只切换运行模式，因此只依赖这一个事实位；模式的稳定语义由
    `RunMode` 承担，不需要 `Agent` 的状态机或装配产物。
    """

    mode: RunMode


class AgentCaller(Protocol):
    """一次工具调用中可读取的调用方事实。

    Attributes:
        uuid: 调用方实例标识，用于事件与进程清理归属。
        mode: 当前运行模式，决定模式级工具可用性。
        agent_type: 调用方类型标识。
        is_subagent: 是否子 agent，决定 MAIN_ONLY 工具边界。
        features: 已启用的 feature 集，决定 feature 级可用性。
        tools: manifest 声明的工具白名单；None 表示未声明。
        history: 当前对话消息，用于提取最近一条用户意图。
    """

    uuid: UUID | str
    mode: RunMode
    agent_type: str
    is_subagent: bool
    features: frozenset[str]
    tools: frozenset[str] | None
    history: list[dict]
