"""SkillToolProvider — 将 SkillManager 适配为 ToolProvider。"""

from src.tools.schemas import ToolDict


class SkillToolProvider:
    """Skill 工具的 Provider 实现"""

    def __init__(self, skill_manager):
        self._manager = skill_manager

    def can_handle(self, tool_name: str) -> bool:
        return tool_name == "activate_skill"

    async def execute(self, tool_name: str, arguments: dict) -> str:
        result = self._manager.activate(arguments.get("name", ""))
        return result if result else "未找到指定的 Skill"

    def get_schemas(self) -> list[ToolDict]:
        schema = self._manager.build_activate_tool_schema()
        return [schema] if schema else []
