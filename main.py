"""Agent 主入口：FSM 驱动的对话循环。

流程：用户输入 → 护栏检查 → Flow 路由（关键词/复杂请求/普通对话）→ FSMRunner 执行
"""

import re
import asyncio

from src.tools import tools, tool_executor
from src.core.async_api import call_model
from src.core.io import agent_input, agent_output
from src.core.fsm import FSMRunner
from src.core.guardrails import InputGuardrail
from src.memory import ConversationBuffer, MemoryStore
from src.flows import detect_flow
from src.flows.planning import PlanningFlow
from src.agents import agent_registry, MultiAgentFlow
from config import USER_ID, MCP_CONFIG_PATH, SKILLS_DIRS
from src.mcp.config import load_mcp_config
from src.mcp.manager import MCPManager
from src.skills import SkillManager

input_guard = InputGuardrail()


def _build_collection_name(prefix: str, user_id: str | None) -> str:
    if not user_id:
        return prefix
    sanitized_user_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized_user_id:
        return prefix
    return f"{prefix}_{sanitized_user_id}"[:63].strip("_")


# 初始化记忆系统
store = MemoryStore(collection_name=_build_collection_name("memories", USER_ID))
buffer = ConversationBuffer(max_rounds=10)


async def is_complex_request(text: str) -> bool:
    """通过 LLM 判断是否为需要多步骤执行的复杂请求。"""
    if text.startswith("/plan"):
        return True
    messages = [
        {"role": "system", "content": "你是一个请求分类器。判断用户的请求是否是一个需要拆解为多个步骤来执行的复杂任务。只回复 yes 或 no。"},
        {"role": "user", "content": text}
    ]
    content, _, _ = await call_model(messages, temperature=0, silent=True)
    return "yes" in content.lower()


async def handle_input(user_input: str, all_tools=None):
    """统一入口：护栏 → Skill 斜杠命令 → Flow 路由 → 执行"""
    effective_tools = all_tools or tools

    # 1. 护栏检查
    passed, reason = input_guard.check(user_input)
    if not passed:
        await agent_output(f"\n[安全拦截] {reason}\n")
        return

    # 2. Skill 斜杠命令检测
    skill_manager = getattr(tool_executor, "skill_manager", None)
    if skill_manager:
        skill_name = skill_manager.is_slash_command(user_input)
        if skill_name:
            skill_content = skill_manager.activate(skill_name)
            if skill_content:
                remaining = user_input[len(f"/{skill_name}"):].strip()
                actual_input = remaining or f"已激活 {skill_name} skill，请按指令执行。"
                multi_agent_flow = MultiAgentFlow(
                    registry=agent_registry,
                    memory=buffer,
                    store=store,
                    all_tools=effective_tools,
                    tool_executor=tool_executor,
                )
                multi_agent_flow.model.data["user_input"] = actual_input
                multi_agent_flow.model.data["skill_content"] = skill_content
                runner = FSMRunner(multi_agent_flow)
                await runner.run()
                return

    # 3. 关键词触发的特殊 Flow（如 /book）
    flow = detect_flow(user_input, tool_executor=tool_executor)
    if flow:
        runner = FSMRunner(flow)
        await runner.run()
        return

    # 3. 复杂请求 → PlanningFlow
    if await is_complex_request(user_input):
        planning_flow = PlanningFlow(
            available_tools=effective_tools,
            tool_executor=tool_executor,
        )
        planning_flow.model.data["original_request"] = user_input
        runner = FSMRunner(planning_flow)
        result = await runner.run()
        if result is not None:
            return
        # result 为 None 表示模型判断不需要计划，回退到普通对话

    # 4. 普通对话 → MultiAgentFlow（总控 + 专业 Agent）
    multi_agent_flow = MultiAgentFlow(
        registry=agent_registry,
        memory=buffer,
        store=store,
        all_tools=effective_tools,
        tool_executor=tool_executor,
    )
    multi_agent_flow.model.data["user_input"] = user_input
    runner = FSMRunner(multi_agent_flow)
    await runner.run()


async def main():
    # 初始化 MCP
    mcp_manager = MCPManager()
    await mcp_manager.connect_all(load_mcp_config(MCP_CONFIG_PATH))

    # 注入 MCP 到现有 tool_executor
    tool_executor.mcp_manager = mcp_manager

    # 初始化 Skills
    skill_manager = SkillManager(skill_dirs=SKILLS_DIRS)
    await skill_manager.discover()
    tool_executor.skill_manager = skill_manager

    # 合并工具列表（本地 tools + MCP tools + Skill 工具）
    mcp_schemas = mcp_manager.get_tools_schemas()
    local_names = {t["function"]["name"] for t in tools}
    for schema in mcp_schemas:
        mcp_name = schema["function"]["name"]
        if mcp_name in local_names:
            print(f"[警告] MCP 工具 '{mcp_name}' 与本地工具同名，可能产生冲突")
    all_tools = tools + mcp_schemas

    # 添加 activate_skill 工具
    activate_schema = skill_manager.build_activate_tool_schema()
    if activate_schema:
        all_tools.append(activate_schema)

    skill_count = len(skill_manager._skills)
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
            await handle_input(user_input, all_tools)
            await agent_output("\n")
    finally:
        await mcp_manager.disconnect_all()


if __name__ == "__main__":
    asyncio.run(main())
