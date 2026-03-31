"""Agent 预设定义与图构建。

orchestrator 的 handoff 列表和路由指令根据传入的 category_summaries
与 business_agents 动态生成，不再硬编码占位 Agent。
"""

from __future__ import annotations

from src.agents.agent import Agent
from src.agents.node import AgentNode
from src.agents.registry import AgentRegistry
from src.agents.context import RunContext
from src.graph.types import NodeResult, CompiledGraph
from src.graph.builder import GraphBuilder

_ORCHESTRATOR_BASE_INSTRUCTIONS = (
    "你是一个智能助手。根据用户的请求选择合适的操作：\n"
    "{handoff_instructions}"
    "- 需要多步骤协作的复杂任务，交给 planner\n"
    "- 其他问题，直接回答用户\n"
)

_PLANNER_AGENT = Agent(
    name="planner",
    description="处理需要多步骤的复杂任务，生成计划并按步骤执行",
    instructions="",
)


def _build_handoff_instructions(
    category_summaries: list[dict[str, str]],
    business_agents: list[dict[str, str]] | None = None,
) -> str:
    """根据分类摘要和业务 Agent 列表生成 handoff 路由指令。"""
    lines: list[str] = []
    for s in category_summaries:
        lines.append(f"- {s['description']}相关，交给 {s['name']}")
    if business_agents:
        for a in business_agents:
            lines.append(f"- {a['description']}相关，交给 {a['name']}")
    return "\n".join(lines) + "\n" if lines else ""


def _make_planner_node_fn():
    async def planner_node_fn(ctx: RunContext) -> NodeResult:
        from src.plan.flow import PlanFlow

        plan_flow = PlanFlow(
            llm=ctx.deps.llm,
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
    category_summaries: list[dict[str, str]] | None = None,
    business_agents: list[dict[str, str]] | None = None,
) -> CompiledGraph:
    """内部构建函数：注册 orchestrator + planner，编译图。"""
    summaries = category_summaries or []
    handoff_instructions = _build_handoff_instructions(summaries, business_agents)
    instructions = _ORCHESTRATOR_BASE_INSTRUCTIONS.format(
        handoff_instructions=handoff_instructions
    )
    if skill_content:
        instructions = f"{skill_content}\n\n{instructions}"

    # 动态构建 handoff 列表
    handoffs = [s["name"] for s in summaries]
    if business_agents:
        handoffs.extend(a["name"] for a in business_agents)
    handoffs.append("planner")

    orchestrator = Agent(
        name="orchestrator",
        description="总控 Agent，负责路由和直接回答",
        instructions=instructions,
        handoffs=handoffs,
    )
    registry.register(orchestrator)
    registry.register(_PLANNER_AGENT)

    builder = GraphBuilder()
    builder.add_node(AgentNode(agent=orchestrator))
    # 为每个 category agent 添加 graph node，使 handoff 可达
    for s in summaries:
        agent = registry.get(s["name"])
        if agent:
            builder.add_node(AgentNode(agent=agent))
    if business_agents:
        for a in business_agents:
            agent = registry.get(a["name"])
            if agent:
                builder.add_node(AgentNode(agent=agent))
    builder.add_function("planner", _make_planner_node_fn())
    builder.set_entry("orchestrator")
    return builder.compile()


def build_default_graph(
    registry: AgentRegistry,
    category_summaries: list[dict[str, str]] | None = None,
    business_agents: list[dict[str, str]] | None = None,
) -> CompiledGraph:
    """构建默认图（无 skill 前缀指令）。"""
    return _register_and_build(
        registry,
        category_summaries=category_summaries,
        business_agents=business_agents,
    )


def build_skill_graph(
    registry: AgentRegistry,
    skill_content: str,
    category_summaries: list[dict[str, str]] | None = None,
    business_agents: list[dict[str, str]] | None = None,
) -> CompiledGraph:
    """构建技能图（skill 内容作为指令前缀）。"""
    return _register_and_build(
        registry,
        skill_content=skill_content,
        category_summaries=category_summaries,
        business_agents=business_agents,
    )
