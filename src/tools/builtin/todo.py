from __future__ import annotations
from typing import TYPE_CHECKING, List, Literal

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class TodoItem(BaseModel):
    content: str = Field(..., description="这一步要做什么")
    status: Literal["pending", "in_progress", "completed"] = Field(..., description="这一步现在处在什么状态")
    active_form: str = Field(..., description="当它正在进行中时，可以用更自然的进行时描述")

class TodoWrite(BaseModel):
    """Rewrite the current session plan for multi-step work."""
    items: List[TodoItem] = Field(..., description="待办事项列表")

@tool(model=TodoWrite, description="Update task tracking list.")
async def todo_write(items: list, agent: Agent) -> str:
    return await agent._todo_mgr.update(items)
