from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import PermissionRule, ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class TaskDelegator(BaseModel):
    description: str = Field(..., description="任务摘要，用于标识这次委派")
    subagent_type: str = Field(..., description="子智能体类型，对应本系统子智能体名称")
    prompt: str = Field(..., description="传给子智能体执行的完整任务正文")

@tool(model=TaskDelegator, description="委托一个任务给子智能体",
      permission=ToolPermission(rules=[PermissionRule(permission="allow")]))
async def task_delegator(description: str, subagent_type: str, prompt: str, agent: Agent) -> str:
    return await agent._subagent_mgr.task_delegator(subagent_type, prompt)
