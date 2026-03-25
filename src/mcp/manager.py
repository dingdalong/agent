import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from .config import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPManager:
    """Manages MCP Server connections, tool discovery, and tool call routing."""

    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}  # full_name -> (server_name, original_name)
        self._timeouts: dict[str, float] = {}
        self._tools_schemas: list[dict] = []

    def _make_tool_name(self, server_name: str, tool_name: str) -> str:
        """Create prefixed tool name: mcp_{server}_{tool}. Hyphens converted to underscores."""
        safe_server = server_name.replace("-", "_")
        return f"mcp_{safe_server}_{tool_name}"

    def _convert_tool_schema(self, server_name: str, tool) -> dict:
        """Convert an MCP Tool to OpenAI function-calling format."""
        full_name = self._make_tool_name(server_name, tool.name)
        description = tool.description or ""
        return {
            "type": "function",
            "function": {
                "name": full_name,
                "description": f"[{server_name}] {description}",
                "parameters": tool.inputSchema,
            }
        }

    def _convert_result(self, result) -> str:
        """Convert a CallToolResult to a plain string."""
        if result.isError:
            texts = [c.text for c in result.content if hasattr(c, "text")]
            return f"工具执行出错: {''.join(texts)}"

        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
            elif hasattr(item, "data"):
                mime = getattr(item, "mimeType", "binary")
                texts.append(f"[{mime} content, {len(item.data)} bytes]")

        output = "\n".join(texts)
        if len(output) > 2000:
            output = output[:2000] + "...(结果已截断)"
        return output

    def get_tools_schemas(self) -> list[dict]:
        """Return all discovered MCP tools in OpenAI format."""
        return list(self._tools_schemas)

    async def connect_all(self, configs: list[MCPServerConfig]) -> None:
        """Connect to all configured MCP Servers. Failures are logged and skipped."""
        for config in configs:
            try:
                await self._connect_one(config)
            except Exception as e:
                logger.warning(f"MCP Server '{config.name}' 连接失败: {e}")

    async def _connect_one(self, config: MCPServerConfig) -> None:
        """Connect to a single MCP Server and discover its tools."""
        logger.info(f"正在连接 MCP Server: {config.name} ({config.transport})")

        if config.transport == "stdio":
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env if config.env else None,
            )
            transport = stdio_client(server_params)
            read_stream, write_stream = await self._exit_stack.enter_async_context(transport)
        else:
            transport = streamablehttp_client(url=config.url)
            # streamablehttp_client yields 3 values: (read, write, get_session_id)
            read_stream, write_stream, _ = await self._exit_stack.enter_async_context(transport)

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        # Discover tools (with pagination)
        tools = []
        cursor = None
        while True:
            result = await session.list_tools(cursor=cursor)
            tools.extend(result.tools)
            if not result.nextCursor:
                break
            cursor = result.nextCursor

        # Register tools
        registered_count = 0
        for tool in tools:
            full_name = self._make_tool_name(config.name, tool.name)
            if full_name in self._tool_map:
                logger.warning(f"MCP 工具名冲突: {full_name}，跳过来自 '{config.name}' 的 '{tool.name}'")
                continue
            self._tool_map[full_name] = (config.name, tool.name)
            self._tools_schemas.append(self._convert_tool_schema(config.name, tool))
            registered_count += 1

        self._sessions[config.name] = session
        self._timeouts[config.name] = config.timeout
        logger.info(f"MCP Server '{config.name}' 已连接，发现 {registered_count} 个工具")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Route a tool call to the appropriate MCP Server session."""
        if tool_name not in self._tool_map:
            return f"错误：未知 MCP 工具 '{tool_name}'"

        server_name, original_name = self._tool_map[tool_name]
        session = self._sessions.get(server_name)
        if not session:
            return f"错误：MCP Server '{server_name}' 未连接"

        timeout = self._timeouts.get(server_name, 30.0)
        try:
            result = await session.call_tool(
                original_name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
            return self._convert_result(result)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"MCP 工具执行出错: {error_msg}"

    async def disconnect_all(self) -> None:
        """Close all MCP Server connections."""
        try:
            await self._exit_stack.aclose()
        except Exception as e:
            logger.warning(f"MCP 断开连接时出错: {e}")
        self._sessions.clear()
        self._tool_map.clear()
        self._tools_schemas.clear()
        logger.info("所有 MCP Server 已断开连接")
