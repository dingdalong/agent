"""AgentNode — 将 Agent 适配为 GraphNode。"""

from __future__ import annotations

from typing import Any

from src.graph.types import NodeResult


class AgentNode:
    """包装一个 Agent，内部用 AgentRunner 驱动。"""

    def __init__(self, agent: Any, runner: Any = None):
        self.name: str = agent.name
        self.agent = agent
        self.runner = runner

    async def execute(self, context: Any) -> NodeResult:
        if self.runner is None:
            raise RuntimeError(f"AgentNode '{self.name}' has no runner assigned")
        result = await self.runner.run(self.agent, context)
        return NodeResult(
            output={"text": result.text, "data": result.data},
            handoff=result.handoff,
        )
