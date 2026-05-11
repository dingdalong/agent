"""UserInputToolProvider — 让 agent 能主动向用户提问。"""
from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import AgentDeps

class AskUser(BaseModel):
    """请确保问题清晰具体，避免模糊的提问。"""
    question: str = Field(description="要向用户提出的问题")

@tool(model=AskUser, description="当你需要用户提供额外信息、做出选择或确认时调用此工具。")
async def ask_user(question: str, deps: AgentDeps) -> str:
    return await deps.event_bus.request_input(f"\n🤖提问: {question}\n你的回答: ")
