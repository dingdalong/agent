"""Agent 主入口：GraphEngine 驱动的对话循环。

流程：用户输入 → 护栏检查 → GraphEngine 执行（orchestrator → specialist handoff）
"""

import re
import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.tools import (
    get_registry, discover_tools,
    ToolExecutor, ToolRouter, LocalToolProvider,
    sensitive_confirm_middleware, truncate_middleware, error_handler_middleware,
)
from src.mcp.provider import MCPToolProvider
from src.skills.provider import SkillToolProvider
from src.core.io import agent_input, agent_output
from src.core.guardrails import InputGuardrail
from src.memory import ConversationBuffer, MemoryStore
from src.agents import (
    Agent, AgentRegistry, GraphBuilder, GraphEngine, RunContext, DictState,
)
from config import USER_ID, MCP_CONFIG_PATH, SKILLS_DIRS
from src.mcp.config import load_mcp_config
from src.mcp.manager import MCPManager
from src.skills import SkillManager

input_guard = InputGuardrail()


# --------------- Deps model ---------------

class AgentDeps(BaseModel):
    """外部依赖：传递 tool_router 给 AgentRunner。"""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    tool_router: Any = None


# --------------- Helper ---------------

def _build_collection_name(prefix: str, user_id: str | None) -> str:
    if not user_id:
        return prefix
    sanitized_user_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized_user_id:
        return prefix
    return f"{prefix}_{sanitized_user_id}"[:63].strip("_")


# --------------- Memory ---------------

store = MemoryStore(collection_name=_build_collection_name("memories", USER_ID))
buffer = ConversationBuffer(max_rounds=10)


# --------------- Agent definitions ---------------

def _build_agents_and_graph(registry: AgentRegistry, skill_content: str | None = None):
    """创建 agent 定义、注册到 registry，构建并编译图。"""
    # Specialist agents
    weather_agent = Agent(
        name="weather_agent",
        description="处理天气查询",
        instructions="你是天气助手。使用 get_weather 工具查询天气信息并回复用户。",
        tools=["get_weather"],
    )
    calendar_agent = Agent(
        name="calendar_agent",
        description="管理日历事件",
        instructions="你是日历助手。使用 create_event 工具帮用户管理日历事件。",
        tools=["create_event"],
    )
    email_agent = Agent(
        name="email_agent",
        description="发送邮件",
        instructions="你是邮件助手。使用 send_email 工具帮用户发送邮件。",
        tools=["send_email"],
    )

    # Orchestrator
    base_instructions = (
        "你是一个智能助手。根据用户的请求选择合适的操作：\n"
        "- 天气相关问题，交给 weather_agent\n"
        "- 日历/日程相关问题，交给 calendar_agent\n"
        "- 邮件相关问题，交给 email_agent\n"
        "- 其他问题，直接回答用户\n"
    )
    if skill_content:
        base_instructions = f"{skill_content}\n\n{base_instructions}"

    orchestrator = Agent(
        name="orchestrator",
        description="总控 Agent，负责路由和直接回答",
        instructions=base_instructions,
        handoffs=["weather_agent", "calendar_agent", "email_agent"],
    )

    # Register
    for ag in [weather_agent, calendar_agent, email_agent, orchestrator]:
        registry.register(ag)

    # Build graph
    graph = (
        GraphBuilder()
        .add_agent("orchestrator", orchestrator)
        .set_entry("orchestrator")
        .compile()
    )
    return graph


# --------------- Handle input ---------------

async def handle_input(
    user_input: str,
    router: ToolRouter,
    engine: GraphEngine,
    graph,
    skill_manager=None,
):
    """统一入口：护栏 → Skill 斜杠命令 → GraphEngine 执行"""
    # 1. 护栏检查
    passed, reason = input_guard.check(user_input)
    if not passed:
        await agent_output(f"\n[安全拦截] {reason}\n")
        return

    # 2. Skill 斜杠命令检测 — 将 skill 内容注入 orchestrator instructions
    skill_content = None
    actual_input = user_input
    if skill_manager:
        skill_name = skill_manager.is_slash_command(user_input)
        if skill_name:
            skill_content = skill_manager.activate(skill_name)
            if skill_content:
                remaining = user_input[len(f"/{skill_name}"):].strip()
                actual_input = remaining or f"已激活 {skill_name} skill，请按指令执行。"
                # 重建带 skill 注入的图
                skill_registry = AgentRegistry()
                skill_graph = _build_agents_and_graph(skill_registry, skill_content)
                skill_engine = GraphEngine(registry=skill_registry)
                ctx = RunContext(
                    input=actual_input,
                    state=DictState(),
                    deps=AgentDeps(tool_router=router),
                )
                result = await skill_engine.run(skill_graph, ctx)
                await agent_output(f"\n{result.output}\n")
                return

    # 3. 正常执行
    ctx = RunContext(
        input=actual_input,
        state=DictState(),
        deps=AgentDeps(tool_router=router),
    )
    result = await engine.run(graph, ctx)

    # 提取输出文本
    output = result.output
    if isinstance(output, dict):
        output = output.get("text", str(output))
    await agent_output(f"\n{output}\n")


# --------------- Main ---------------

async def main():
    # 1. 发现并注册本地工具
    discover_tools("src.tools.builtin", Path("src/tools/builtin"))

    # 2. 构建本地工具执行管道
    registry = get_registry()
    executor = ToolExecutor(registry)
    middlewares = [
        error_handler_middleware(),
        sensitive_confirm_middleware(registry),
        truncate_middleware(2000),
    ]
    local_provider = LocalToolProvider(registry, executor, middlewares)

    # 3. 构建路由器
    router = ToolRouter()
    router.add_provider(local_provider)

    # 4. 初始化 MCP
    mcp_manager = MCPManager()
    await mcp_manager.connect_all(load_mcp_config(MCP_CONFIG_PATH))
    mcp_schemas = mcp_manager.get_tools_schemas()
    if mcp_schemas:
        router.add_provider(MCPToolProvider(mcp_manager))

    # 5. 初始化 Skills
    skill_manager = SkillManager(skill_dirs=SKILLS_DIRS)
    await skill_manager.discover()
    skill_count = len(skill_manager._skills)
    if skill_count:
        router.add_provider(SkillToolProvider(skill_manager))

    # 6. 构建 agent 注册表、图、引擎
    agent_reg = AgentRegistry()
    default_graph = _build_agents_and_graph(agent_reg)
    engine = GraphEngine(registry=agent_reg)

    print("Agent 已启动，输入 'exit' 退出。")
    if mcp_schemas:
        print(f"已加载 {len(mcp_schemas)} 个 MCP 工具")
    if skill_count:
        print(f"已发现 {skill_count} 个 Skill")

    try:
        while True:
            user_input = await agent_input("\n你: ")
            if user_input.lower() in ["exit", "quit"]:
                break
            await handle_input(user_input, router, engine, default_graph, skill_manager)
            await agent_output("\n")
    finally:
        await mcp_manager.disconnect_all()


if __name__ == "__main__":
    asyncio.run(main())
