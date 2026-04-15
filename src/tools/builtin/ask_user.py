"""UserInputToolProvider — 让 agent 能主动向用户提问。"""
from src.tools.decorator import tool
from pydantic import BaseModel, Field
from typing import Any
from src.singleton import ui

class AsyncCalculator(BaseModel):
    """请确保问题清晰具体，避免模糊的提问。"""
    question: str = Field(description="要向用户提出的问题")

@tool(model=AsyncCalculator, description="当你需要用户提供额外信息、做出选择或确认时调用此工具。")
async def ask_user(question: str) -> str:
    return await ui.ask(question)
