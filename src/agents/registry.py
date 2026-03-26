"""AgentRegistry — Agent 注册表。"""

from __future__ import annotations

from typing import Optional

from src.agents.agent import Agent


class AgentRegistry:
    """管理所有已注册的 Agent。"""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        """注册一个 Agent（同名覆盖）。"""
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[Agent]:
        """根据名称获取 Agent，不存在返回 None。"""
        return self._agents.get(name)

    def has(self, name: str) -> bool:
        """检查 Agent 是否已注册。"""
        return name in self._agents

    def all_agents(self) -> list[Agent]:
        """返回所有已注册的 Agent 列表。"""
        return list(self._agents.values())
