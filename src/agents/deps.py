"""AgentDeps — Agent 运行时外部依赖模型。"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentDeps(BaseModel):
    """外部依赖：传递给 AgentRunner、PlanFlow 等组件。

    Attributes:
        tool_router: 工具路由器
        agent_registry: Agent 注册表
        graph_engine: 图执行引擎
        ui: UserInterface 实例，用于 I/O 操作
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    tool_router: Any = None
    agent_registry: Any = None
    graph_engine: Any = None
    ui: Any = None
