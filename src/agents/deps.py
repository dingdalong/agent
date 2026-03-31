"""AgentDeps — Agent 运行时外部依赖模型。"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentDeps(BaseModel):
    """外部依赖：传递给 AgentRunner、PlanFlow 等组件。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    llm: Any = None              # LLMProvider
    tool_router: Any = None      # ToolRouter
    agent_registry: Any = None   # AgentRegistry
    graph_engine: Any = None     # GraphEngine
    ui: Any = None               # UserInterface
    memory: Any = None           # MemoryProvider
    runner: Any = None           # AgentRunner
