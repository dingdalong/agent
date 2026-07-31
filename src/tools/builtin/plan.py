"""计划工作流工具 — 进入/退出计划模式、设置计划文件。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from src.events.types import caller_identity
from src.tools.decorator import ToolPermission, tool

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps


# ── enter_plan_mode ─────────────────────────────────────────────────


class EnterPlanMode(BaseModel):
    """无参数。"""
    pass


@tool(
    model=EnterPlanMode,
    description=(
        "切换到计划模式，用于在实施前进行结构化规划。计划模式下仅允许只读操作和计划文件的写入。\n\n"
        "何时使用：\n"
        "- 用户明确要求制定计划或进入计划模式\n"
        "- 任务涉及多个文件或模块、需要先理解后实施\n"
        "- 存在多种可行方案、需要探索和设计（如架构选型、缓存策略）\n"
        "- 需求不明确，需先探索代码库再确定实现路径\n"
        "- 用户请求较复杂（超过 3 个步骤）且你不确定最佳路径\n\n"
        "何时不使用：\n"
        "- 简单的单文件修改或明确的小任务\n"
        "- 用户明确要求直接实施、不要计划\n"
        "- 纯研究或探索任务（用子智能体即可）\n"
        "- 已在计划模式中"
    ),
    permission=ToolPermission(kind="readonly"),
    subagent=False,
    feature="plan",
)
async def enter_plan_mode(agent: Agent, deps: AgentDeps) -> str:
    """将当前 agent 切换到 PLAN_MODE。

    Args:
        agent: 当前 Agent 实例，持有 permission_mode。
        deps: AgentDeps 依赖对象，提供 plan_mgr。

    Returns:
        操作结果描述。
    """
    plan_mgr = deps.plan_mgr
    if plan_mgr is None:
        return "错误：计划管理器不可用"

    if not plan_mgr.enter_mode(agent, agent._reminder_mgr):
        return "已在计划模式中。"

    return "已进入计划模式。"


# ── set_plan_file ──────────────────────────────────────────────────


class SetPlanFile(BaseModel):
    """设置计划文件的参数。"""
    file_path: str = Field(..., description="计划文件的绝对路径。")


@tool(
    model=SetPlanFile,
    description=(
        "设置当前正在编辑的计划文件路径。在写入计划文件后调用此工具，"
        "使后续提示词能引用当前计划。"
    ),
    permission=ToolPermission(kind="readonly"),
    subagent=False,
    feature="plan",
)
def set_plan_file(file_path: str, deps: AgentDeps) -> str:
    """记录当前活跃的计划文件路径。

    Args:
        file_path: 计划文件的绝对路径。
        deps: AgentDeps 依赖对象，提供 plan_mgr。

    Returns:
        操作结果描述。
    """
    plan_mgr = deps.plan_mgr
    if plan_mgr is None:
        return "错误：计划管理器不可用"

    plan_mgr.set_active_plan_path(file_path)
    return f"已设置当前计划文件：{file_path}"


# ── exit_plan_mode ──────────────────────────────────────────────────


class ExitPlanMode(BaseModel):
    """退出计划模式的参数。"""
    file_path: str = Field(..., description="计划文件的绝对路径。")


@tool(
    model=ExitPlanMode,
    description=(
        "退出计划模式并提交计划供用户审核。传入计划文件路径，展示计划内容，用户可选择自动执行、手动执行或返回修改意见。\n\n"
        "使用要求：\n"
        "- 必须先通过 write_file 写入计划文件，再调用此工具\n"
        "- 不要用 ask_user 询问\"计划可以吗\"——提交审核必须用此工具\n"
        "- 如果用户返回修改意见，根据意见修改计划后再次提交"
    ),
    permission=ToolPermission(kind="readonly"),
    subagent=False,
    feature="plan",
)
async def exit_plan_mode(file_path: str, agent: Agent, deps: AgentDeps) -> str:
    """校验当前处于 plan 模式、读取计划内容、让用户选择后续操作。

    Args:
        file_path: 计划文件的绝对路径，由 LLM 提供。
        agent: 当前 Agent 实例，持有 permission_mode。
        deps: AgentDeps 依赖对象，提供 plan_mgr。

    Returns:
        用户选择的操作结果和后续指引。
    """
    from src.mgr.permission_mgr import AUTO_MODE, PLAN_MODE

    if agent.permission_mode is not PLAN_MODE:
        return "错误：当前不在计划模式中。"

    plan_mgr = deps.plan_mgr
    if plan_mgr is None:
        return "错误：计划管理器不可用"

    reminder_mgr = agent._reminder_mgr

    plan_file = Path(file_path)
    if not plan_file.is_file():
        plan_mgr.exit_mode(agent, reminder_mgr)
        return f"计划文件不存在，已退出计划模式，恢复到 {agent.permission_mode.value} 模式。"

    # 阻塞读取卸载到线程，避免占用事件循环（exit_plan_mode 须保持 async，因其还 await 事件总线）。
    plan_content = await asyncio.to_thread(plan_file.read_text, encoding="utf-8")
    if not plan_content.strip():
        plan_mgr.exit_mode(agent, reminder_mgr)
        return f"计划为空，已退出计划模式，恢复到 {agent.permission_mode.value} 模式。"

    # 表头（路径/标签）为结构化 chrome 走纯文本；计划正文是 LLM 写的 Markdown，单独按 Markdown 渲染。
    await deps.event_bus.request_output(f"\n计划文件：\n{file_path}\n\n计划内容：\n")
    await deps.event_bus.request_output(plan_content, markdown=True)

    # choice_input 语义：选项行提交→choice=该项 value（auto/manual）、feedback 为空；
    # 输入行提交→choice 为空、feedback=修改意见；Esc 取消→两者皆空。三者互斥、以光标所在行为准。
    caller_agent_type, caller_uuid = caller_identity(agent)
    choice, feedback = await deps.event_bus.request_choice_input(
        prompt="计划审核",
        options=[("auto", "自动执行"), ("manual", "手动执行")],
        descriptions=["在当前上下文中自动实施计划", "退出计划模式，自行实施"],
        input_placeholder="输入修改意见…",
        default_index=0,
        markdown=False,
        caller_agent_type=caller_agent_type,
        caller_uuid=caller_uuid,
    )
    feedback = feedback.strip()

    if choice == "auto":
        plan_mgr.exit_mode(agent, reminder_mgr)
        agent.set_permission_mode(AUTO_MODE)
        return (
            f"用户已批准计划，选择自动执行。已切换到 auto 模式。\n\n"
            f"计划文件路径：{file_path}\n\n"
            f"## 已批准的计划：\n{plan_content}"
        )

    if choice == "manual":
        plan_mgr.exit_mode(agent, reminder_mgr)
        return (
            f"用户已批准计划，选择手动执行。已恢复到 {agent.permission_mode.value} 模式。\n\n"
            f"计划文件路径：{file_path}\n\n"
            f"## 已批准的计划：\n{plan_content}"
        )

    if feedback:  # 输入行提交修改意见：计划模式保持不变
        return f"用户对计划的修改意见：{feedback}\n请根据以上意见与用户进一步沟通需求。"

    # choice 与 feedback 皆空：用户按 Esc 取消，不退出计划模式
    return "用户取消了操作，仍处于计划模式。可继续完善计划或再次提交。"
