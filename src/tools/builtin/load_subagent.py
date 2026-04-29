from __future__ import annotations
from typing import TYPE_CHECKING, List, Literal

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class TaskDelegator(BaseModel):
    name: str = Field(..., description="要加载的子智能体")
    prompt: str = Field(..., description="简短的任务需求描述")

@tool(model=TaskDelegator, description="委托一个任务给子智能体")
async def task_delegator(name: str, prompt: str, agent: Agent) -> str:
    return await agent._subagent_mgr.task_delegator(name, prompt)
