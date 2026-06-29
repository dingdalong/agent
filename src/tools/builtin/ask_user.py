"""UserInputToolProvider — 让 agent 能主动向用户提问。"""
from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import AgentDeps

class AskUser(BaseModel):
    """请确保问题清晰具体，避免模糊的提问。"""
    question: str = Field(description="要向用户提出的问题")
    options: list[str] | None = Field(
        default=None,
        description="可选项列表；提供时用户以方向键菜单从中选择并返回选中项，不提供则由用户自由文本作答",
    )

@tool(model=AskUser,
      description="当你需要用户提供额外信息、做出选择或确认时调用此工具。问题为固定选项时传 options（用户用方向键菜单选择）；开放式回答则省略 options。",
      permission=ToolPermission(kind="readonly"), subagent=False)
async def ask_user(question: str, options: list[str] | None, deps: AgentDeps) -> str:
    """向用户提问并返回回答。

    Args:
        question: 要向用户提出的问题。
        options: 可选项列表；非空时弹出方向键选择菜单，否则读取自由文本。
        deps: Agent 依赖容器，提供事件总线。
    Returns:
        用户的回答文本；带 options 且用户取消（Esc）时返回取消哨兵串。
    """
    if options:
        answer = await deps.event_bus.request_choice(
            f"🤖 **提问**\n\n{question}", [(opt, opt) for opt in options], 0, markdown=True
        )
        return answer or "[用户取消了选择，未作答]"
    return await deps.event_bus.request_input(f"🤖 **提问**\n\n{question}\n你的回答: ", markdown=True)
