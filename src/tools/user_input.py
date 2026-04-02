"""UserInputToolProvider — 让 agent 能主动向用户提问。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from src.tools.schemas import ToolDict

if TYPE_CHECKING:
    from src.utils.interaction import UserInteractionService


class UserInputToolProvider:
    """让 agent 能主动向用户提问的 ToolProvider。

    实现 ToolProvider 协议，注册到 ToolRouter 后，
    agent 可通过调用 ask_user 工具向用户提出问题。
    """

    def __init__(self, interaction: UserInteractionService) -> None:
        self._interaction = interaction

    def can_handle(self, tool_name: str) -> bool:
        return tool_name == "ask_user"

    def get_schemas(self) -> list[ToolDict]:
        return [{
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "当你需要用户提供额外信息、做出选择或确认时调用此工具。"
                    "请确保问题清晰具体，避免模糊的提问。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "要向用户提出的问题",
                        },
                    },
                    "required": ["question"],
                },
            },
        }]

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: Any = None,
    ) -> str:
        question = arguments.get("question", "")
        if not question:
            return "错误：question 参数不能为空"
        source = ""
        if context is not None:
            source = getattr(context, "current_agent", "")
        return await self._interaction.ask(question, source=source)
