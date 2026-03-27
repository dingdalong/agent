"""Agent 定义与图构建。

提供默认 agent 集合（orchestrator + specialist agents + planner）的定义和图构建。
"""

from __future__ import annotations

from src.agents.agent import Agent
from src.agents.registry import AgentRegistry
from src.agents.context import RunContext
from src.agents.graph.types import NodeResult, CompiledGraph
from src.agents.graph.builder import GraphBuilder


_ORCHESTRATOR_BASE_INSTRUCTIONS = (
    "你是一个智能助手。根据用户的请求选择合适的操作：\n"
    "- 天气相关问题，交给 weather_agent\n"
    "- 日历/日程相关问题，交给 calendar_agent\n"
    "- 邮件相关问题，交给 email_agent\n"
    "- 需要多步骤协作的复杂任务（如查天气然后发邮件），交给 planner\n"
    "- 其他问题，直接回答用户\n"
)

_SPECIALIST_AGENTS = [
    Agent(
        name="weather_agent",
        description="处理天气查询",
        instructions="你是天气助手。使用 get_weather 工具查询天气信息并回复用户。",
        tools=["get_weather"],
    ),
    Agent(
        name="calendar_agent",
        description="管理日历事件",
        instructions="你是日历助手。使用 create_event 工具帮用户管理日历事件。",
        tools=["create_event"],
    ),
    Agent(
        name="email_agent",
        description="发送邮件",
        instructions="你是邮件助手。使用 send_email 工具帮用户发送邮件。",
        tools=["send_email"],
    ),
]

_PLANNER_AGENT = Agent(
    name="planner",
    description="处理需要多步骤的复杂任务，生成计划并按步骤执行",
    instructions="",  # 不会被 AgentRunner 使用，由 FunctionNode 接管
)


def _make_planner_node_fn():
    """创建 planner 的 FunctionNode 执行函数。

    通过 ctx.deps 延迟获取 PlanFlow 所需的依赖，避免循环导入。
    """

    async def planner_node_fn(ctx: RunContext) -> NodeResult:
        from src.plan.flow import PlanFlow

        plan_flow = PlanFlow(
            tool_router=ctx.deps.tool_router,
            agent_registry=ctx.deps.agent_registry,
            engine=ctx.deps.graph_engine,
            ui=ctx.deps.ui,
        )
        result = await plan_flow.run(ctx.input)
        return NodeResult(output=result)

    return planner_node_fn


def _register_and_build(
    registry: AgentRegistry,
    skill_content: str | None = None,
) -> CompiledGraph:
    """注册 agents 并构建图。内部共享逻辑。"""
    # 注册 specialist agents
    for agent in _SPECIALIST_AGENTS:
        registry.register(agent)

    # 构建 orchestrator instructions
    instructions = _ORCHESTRATOR_BASE_INSTRUCTIONS
    if skill_content:
        instructions = f"{skill_content}\n\n{instructions}"

    orchestrator = Agent(
        name="orchestrator",
        description="总控 Agent，负责路由和直接回答",
        instructions=instructions,
        handoffs=["weather_agent", "calendar_agent", "email_agent", "planner"],
    )
    registry.register(orchestrator)
    registry.register(_PLANNER_AGENT)

    # 构建图
    graph = (
        GraphBuilder()
        .add_agent("orchestrator", orchestrator)
        .add_function("planner", _make_planner_node_fn())
        .set_entry("orchestrator")
        .compile()
    )
    return graph


def build_default_graph(registry: AgentRegistry) -> CompiledGraph:
    """构建默认 agent 图（无 skill 注入）。"""
    return _register_and_build(registry)


def build_skill_graph(registry: AgentRegistry, skill_content: str) -> CompiledGraph:
    """构建带 skill 内容注入的 agent 图。"""
    return _register_and_build(registry, skill_content=skill_content)
