"""AgentNode — 将 Agent 适配为 GraphNode。"""

from __future__ import annotations

from typing import Any

from src.graph.types import NodeResult


class AgentNode:
    """包装一个 Agent，通过 context.deps.runner 驱动。"""

    def __init__(self, agent: Any):
        self.name: str = agent.name
        self.agent = agent

    async def execute(self, context: Any) -> NodeResult:
        runner = getattr(context.deps, "runner", None)
        if runner is None:
            raise RuntimeError(f"AgentNode({self.name}): deps.runner is None")
        result = await runner.run(self.agent, context)
        return NodeResult(
            output={"text": result.text, "data": result.data},
            handoff=result.handoff,
        )
