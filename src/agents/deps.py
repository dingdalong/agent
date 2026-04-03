"""AgentDeps — Agent 运行时外部依赖模型。

类型约定（运行时为 Any，由 bootstrap.py 通过 isinstance 断言保证）：
- llm: LLMProvider (src.llm.base)
- tool_router: ToolRouter (src.tools.router)
- agent_registry: AgentRegistry (src.agents.registry)
- graph_engine: GraphEngine (src.graph.engine)
- ui: UserInterface (src.interfaces.base)
- memory: MemoryProvider | None (src.memory.base)
- runner: AgentRunner (src.agents.runner)
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentDeps(BaseModel):
    """外部依赖：传递给 AgentRunner、PlanFlow 等组件。

    所有字段声明为 Any 以避免循环导入（deps.py 在 Layer 2，
    依赖组件分布在 Layer 0-3）。实际类型由 bootstrap.py 在
    组装时通过 isinstance 断言保证。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    llm: Any = None              # LLMProvider — 必需
    tool_router: Any = None      # ToolRouter — 必需
    agent_registry: Any = None   # AgentRegistry — 必需
    graph_engine: Any = None     # GraphEngine — 必需
    ui: Any = None               # UserInterface — 必需
    memory: Any = None           # MemoryProvider — 可选
    runner: Any = None           # AgentRunner — 必需
