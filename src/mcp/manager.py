import asyncio
import logging
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import ListRootsResult, Root

from .config import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPManager:
    """Manages MCP Server connections, tool discovery, and tool call routing."""

    def __init__(self, configs: list[MCPServerConfig] | None = None):
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}  # full_name -> (server_name, original_name)
        self._timeouts: dict[str, float] = {}
        self._tools_schemas: list[dict] = []
        # 按 safe_name 存储配置，供 connect_server 按需连接使用
        self._configs: dict[str, MCPServerConfig] = {
            re.sub(r"[^a-zA-Z0-9_]", "_", cfg.name): cfg
            for cfg in (configs or [])
        }
        self._connect_locks: dict[str, asyncio.Lock] = {}

    async def connect_server(self, safe_name: str) -> None:
        """按需连接单个 MCP Server（幂等，并发安全）。

        已连接的 server 直接返回；未知 safe_name 抛出 KeyError。
        使用 per-server Lock 防止并发重复连接。
        """
        if safe_name in self._sessions:
            return
        if safe_name not in self._configs:
            raise KeyError(f"未知 MCP Server: '{safe_name}'")
        lock = self._connect_locks.setdefault(safe_name, asyncio.Lock())
        async with lock:
            if safe_name in self._sessions:
                return
            await self._connect_one(self._configs[safe_name])

    def _make_tool_name(self, server_name: str, tool_name: str) -> str:
        """Create prefixed tool name: mcp_{server}_{tool}. Non-alphanumeric chars converted to underscores."""
        safe_server = re.sub(r"[^a-zA-Z0-9_]", "_", server_name)
        safe_tool = re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)
        return f"mcp_{safe_server}_{safe_tool}"

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
        if not output:
            return "(执行成功，无输出)"
        if len(output) > 2000:
            output = output[:2000] + "...(结果已截断)"
        return output

    def get_tools_schemas(self) -> list[dict]:
        """Return all discovered MCP tools in OpenAI format."""
        return list(self._tools_schemas)

    async def ensure_servers_for_tools(self, tool_names: list[str]) -> None:
        """按需连接工具名对应的 MCP Server（幂等）。

        仅处理以 "mcp_" 开头的工具名；非 MCP 工具静默忽略。
        使用最长前缀匹配避免 "foo" 与 "foo_bar" 歧义：按 safe_name 长度降序
        检查每个已配置 server 的 "mcp_{safe_name}_" 前缀是否匹配工具名。
        连接失败的 server 会被记录并跳过，不影响其他 server。
        """
        # 按 safe_name 长度降序排列，确保最长前缀优先匹配
        sorted_keys = sorted(self._configs.keys(), key=len, reverse=True)

        needed: set[str] = set()
        for tool_name in tool_names:
            if not tool_name.startswith("mcp_"):
                continue
            for safe_name in sorted_keys:
                if tool_name.startswith(f"mcp_{safe_name}_"):
                    if safe_name not in self._sessions:
                        needed.add(safe_name)
                    break  # 最长前缀已找到，不再继续

        for safe_name in needed:
            try:
                await self.connect_server(safe_name)
            except Exception as e:
                config = self._configs.get(safe_name)
                name = config.name if config else safe_name
                logger.warning(f"MCP Server '{name}' 按需连接失败: {e}")

    async def connect_all(self, connect_timeout: float = 30.0) -> None:
        """Connect to all configured MCP Servers. Failures are logged and skipped."""
        for config in self._configs.values():
            try:
                await asyncio.wait_for(self._connect_one(config), timeout=connect_timeout)
            except asyncio.TimeoutError:
                logger.warning(f"MCP Server '{config.name}' 连接超时 ({connect_timeout}s)，跳过")
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

        # 构建 roots 回调，让 server 知道客户端授权的目录
        roots_callback = None
        if config.roots:
            roots = [
                Root(uri=Path(r).resolve().as_uri(), name=r)
                for r in config.roots
            ]
            async def roots_callback(_ctx: Any) -> ListRootsResult:
                return ListRootsResult(roots=roots)

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream, list_roots_callback=roots_callback)
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

        # Register tools — 统一使用 safe_name 作为内部 key
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", config.name)
        registered_count = 0
        for tool in tools:
            full_name = self._make_tool_name(config.name, tool.name)
            if full_name in self._tool_map:
                logger.warning(f"MCP 工具名冲突: {full_name}，跳过来自 '{config.name}' 的 '{tool.name}'")
                continue
            self._tool_map[full_name] = (safe_name, tool.name)
            self._tools_schemas.append(self._convert_tool_schema(config.name, tool))
            registered_count += 1

        self._sessions[safe_name] = session
        self._timeouts[safe_name] = config.timeout
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
