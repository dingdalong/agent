from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.policy import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class LoadSkill(BaseModel):
    name: str = Field(..., description="要加载的技能")

@tool(model=LoadSkill, description="将指定技能的完整内容加载到当前上下文中。",
      policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True), subagent=False, feature="skill")
async def load_skill(name: str, agent: Agent) -> str:
    return agent._skill_mgr.load_full_text(name)
