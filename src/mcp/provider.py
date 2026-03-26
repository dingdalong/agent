"""MCPToolProvider — 将 MCPManager 适配为 ToolProvider。"""

from src.tools.schemas import ToolDict


class MCPToolProvider:
    """MCP 工具的 Provider 实现"""

    def __init__(self, mcp_manager):
        self._manager = mcp_manager

    def can_handle(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp_")

    async def execute(self, tool_name: str, arguments: dict) -> str:
        return await self._manager.call_tool(tool_name, arguments)

    def get_schemas(self) -> list[ToolDict]:
        return self._manager.get_tools_schemas()
