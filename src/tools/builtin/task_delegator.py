from __future__ import annotations
from typing import Literal, TYPE_CHECKING

from src.tools.policy import AccessKind, DataFlow, ToolAudience, ToolAvailability, ToolPolicy
from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class TaskDelegator(BaseModel):
    description: str = Field(
        ...,
        description="任务摘要，用于标识这次委派；同时作为共享上下文条目的标题，请写清这次委派要解决什么",
    )
    agent_type: str = Field(..., description="智能体类型，对应本系统智能体类型标识")
    prompt: str = Field(..., description="传给子智能体执行的完整任务正文")
    task_id: str | None = Field(
        default=None,
        description="关联的任务 ID。指定后框架自动将任务标记为 in_progress 并设置 owner",
    )
    shared_context: Literal["auto", "none"] = Field(
        default="auto",
        description=(
            "是否把本会话已积累的共享上下文（其他子任务已核实的事实）自动注入子智能体。"
            "auto=注入（默认）；none=完全隔离，仅在需要独立复核、避免先前结论影响判断时使用"
        ),
    )

@tool(model=TaskDelegator, description="委托一个任务给子智能体",
      policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True, detail_template="委托 {agent_type}"),
      availability=ToolAvailability(feature="subagent", audience=ToolAudience.MAIN_ONLY), counts_as_work=False)
async def task_delegator(
    description: str,
    agent_type: str,
    prompt: str,
    agent: Agent,
    task_id: str | None = None,
    shared_context: str = "auto",
) -> str:
    """委派任务给子智能体并返回执行结果。

    Args:
        description: 任务摘要，用于标识委派，并作为共享上下文条目的标题。
        agent_type: 目标子智能体类型标识。
        prompt: 传给子智能体的完整任务正文。
        agent: 当前 Agent 实例（自动注入）。
        task_id: 关联的任务 ID（可选），指定后框架自动管理任务状态。
        shared_context: "auto" 注入共享上下文；"none" 完全隔离用于独立复核。

    Returns:
        子智能体的执行结果文本。
    """
    return await agent._subagent_mgr.task_delegator(
        agent_type, prompt, parent_agent=agent, task_id=task_id,
        description=description, shared_context=shared_context,
    )
