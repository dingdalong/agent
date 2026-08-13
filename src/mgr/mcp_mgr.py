"""MCP 客户端管理器 — 连接配置的 MCP server，发现其工具并注册进 ToolsMgr。

每个 server 在专属的常驻 asyncio 任务里打开连接（transport + ClientSession 的 async 上下文），
该任务持续等待停止信号后才退出上下文——保证上下文的进入与退出在同一任务，规避 anyio
cancel-scope 跨任务退出错误，同时允许多个 server 并发连接。工具调用从其它任务复用同一
session 的消息流（anyio 流跨任务收发安全）。所有方法只 await 真异步原语，满足异步/阻塞契约。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from src.mgr.frozen import clean_env
from src.tools import AccessKind, DataFlow, ToolOrigin, ToolPolicy
from src.tools.decorator import ToolEntry

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager
    from src.mgr.tools_mgr import ToolsMgr
    from src.mgr.role_mgr import RoleMgr

logger = logging.getLogger(__name__)

# 单 server 连接（含初始化握手）超时秒数
_CONNECT_TIMEOUT = 30.0
# 停止时等待所有 server 任务清退的总超时秒数
_CLOSE_TIMEOUT = 5.0
# 工具名长度上限（对齐 Anthropic 工具名约束）
_MAX_TOOL_NAME = 64


class _PassThroughArgs(BaseModel):
    """MCP 工具的透传参数模型 — model_dump() 原样回吐全部入参，不做字段级校验。"""
    model_config = ConfigDict(extra="allow")


@dataclass
class _ServerConn:
    """单个 MCP server 的运行态句柄。

    Attributes:
        name: server 名。
        session: 已初始化的 ClientSession。
        tool_names: 该 server 注册进 ToolsMgr 的工具名列表。
    """
    name: str
    session: Any
    tool_names: list[str] = field(default_factory=list)


def _safe_tool_name(raw: str) -> str:
    """将工具名清洗为 [A-Za-z0-9_-] 字符集并限长，符合 provider 工具名约束。

    Args:
        raw: 原始工具名（形如 mcp__server__tool）。

    Returns:
        清洗并截断到 64 字符内的工具名。
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:_MAX_TOOL_NAME]


def _interpolate_env(env: dict[str, Any], workdir: Path) -> dict[str, str]:
    """替换 MCP server env 各值中的路径占位符为运行时绝对路径。

    子进程 env 的值原样透传给子进程，框架在此先做占位符替换，使配置能引用随运行
    变化的路径。支持两种占位符，其余内容原样保留：
    - ``${workdir}`` → 当前工作目录（`--workdir` 指定的目标仓库）的绝对路径。
    - 开头的 ``~`` → 用户家目录。
    所有值统一转为字符串（子进程 env 仅接受字符串键值）。

    Args:
        env: 某 server 在 mcp_servers.json 中声明的 env 字典，值可含占位符。
        workdir: 当前工作目录，用于替换 ``${workdir}``。

    Returns:
        占位符替换并展开后的 env 字典，键值均为字符串。
    """
    resolved: dict[str, str] = {}
    for key, value in env.items():
        text = str(value).replace("${workdir}", str(workdir))
        if text.startswith("~"):
            text = str(Path(text).expanduser())
        resolved[key] = text
    return resolved


def _format_result(result: Any) -> str:
    """将 MCP CallToolResult 转为字符串结果。

    拼接 content 中的文本块；非文本块（图片/嵌入资源）以占位说明替代（当前框架纯文本）。
    isError 为真时以 "错误：" 前缀，命中 ToolsMgr 的错误状态判定。

    Args:
        result: ClientSession.call_tool 返回的 CallToolResult。

    Returns:
        格式化后的结果字符串。
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[非文本内容: {getattr(block, 'type', 'unknown')}]")
    if not parts and getattr(result, "structuredContent", None):
        parts.append(str(result.structuredContent))
    text = "\n".join(parts)
    if getattr(result, "isError", False):
        return f"错误：MCP 工具返回错误：{text}"
    return text


class McpMgr:
    """MCP 客户端管理器 — 启动时连接 server 并注册工具，关闭时统一断开。

    Args:
        config_mgr: 配置管理器，用于读取合并后的 mcp_servers.json。
        tools_mgr: 工具管理器，发现的 MCP 工具注册到此。
        role_mgr: 角色管理器，为 None 时跳过角色层 MCP 配置。
        workdir: 当前工作目录，用于展开各 server env 值中的 ``${workdir}`` 占位符。
    """

    def __init__(
        self,
        config_mgr: ConfigManager,
        tools_mgr: ToolsMgr,
        role_mgr: RoleMgr | None = None,
        workdir: Path | None = None,
        data_guard: Any = None,
        project_trusted: bool = False,
    ) -> None:
        self.config_mgr = config_mgr
        self.tools_mgr = tools_mgr
        self.role_mgr = role_mgr
        self.workdir = workdir or Path.cwd()
        self.data_guard = data_guard
        self.project_trusted = project_trusted
        self._conns: dict[str, _ServerConn] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        """读取配置，为每个 MCP server 启动常驻连接任务并等待其就绪。

        三层 MCP 配置合并（低→高）：角色 → global → project。
        MCP 工具始终注册为 REVIEW + EXTERNAL。
        单个 server 连接失败仅记日志并跳过，不影响其它 server 与主流程。
        未配置 server 或未安装 mcp 包时整体跳过。
        """
        # 三层合并：role → global → project
        servers: dict[str, dict[str, Any]] = {}
        # 角色层
        if self.role_mgr is not None and self.role_mgr.active:
            sp = self.role_mgr.mcp_servers_path()
            role_is_project = sp is not None and sp.is_relative_to(self.workdir)
            if sp is not None and (self.project_trusted or not role_is_project):
                try:
                    role_data = json.loads(sp.read_text()).get("mcpServers", {})
                    if isinstance(role_data, dict):
                        servers.update(role_data)
                except (json.JSONDecodeError, OSError):
                    pass
        # global + project（project 覆盖 global，二者均覆盖角色层同名 key）
        servers.update(self.config_mgr.load_mcp_servers())
        # server 级开关：settings.json 的 mcp.enabledServers（非空则白名单）与 mcp.disabledServers（始终剔除）
        servers = self._apply_server_policy(servers)
        if not servers:
            return
        try:
            __import__("mcp")  # 懒导入：缺包时跳过 MCP 接入，保证应用仍可运行。
        except ImportError:
            logger.warning("未安装 mcp 包，跳过 MCP server 接入（uv add mcp）")
            return

        self._stop_event = asyncio.Event()
        ready_events: list[asyncio.Event] = []
        for name, spec in servers.items():
            ready = asyncio.Event()
            ready_events.append(ready)
            self._tasks.append(asyncio.create_task(self._serve(name, spec, ready)))
        await asyncio.gather(*(ready.wait() for ready in ready_events))

        tool_count = sum(len(c.tool_names) for c in self._conns.values())
        logger.info(
            "已连接 %d/%d 个 MCP server，注册 %d 个工具",
            len(self._conns), len(servers), tool_count,
        )

    def _apply_server_policy(self, servers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """按 settings.json 的 mcp 策略过滤待连接的 server 集合。

        策略来自合并后的 settings.json 的 mcp 段：
        - enabledServers：非空时作为白名单，只保留其中的 server。
        - disabledServers：始终从结果中剔除。
        被跳过的 server 记一条日志。

        Args:
            servers: 三层合并后的 server 配置（server 名 → 连接配置）。

        Returns:
            过滤后的 server 配置。
        """
        policy = self.config_mgr.get_user_setting("mcp")
        if not isinstance(policy, dict):
            return servers
        enabled = set(policy.get("enabledServers") or [])
        disabled = set(policy.get("disabledServers") or [])
        if not enabled and not disabled:
            return servers
        kept = {
            name: spec
            for name, spec in servers.items()
            if (not enabled or name in enabled) and name not in disabled
        }
        skipped = [name for name in servers if name not in kept]
        if skipped:
            logger.info("按 mcp 策略跳过 %d 个 server：%s", len(skipped), ", ".join(skipped))
        return kept

    async def stop(self) -> None:
        """通知所有 server 任务退出并等待其清退连接，超时则强制取消。"""
        if self._stop_event is None:
            return
        self._stop_event.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=_CLOSE_TIMEOUT)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._conns.clear()
        self._stop_event = None

    async def _serve(self, name: str, spec: dict[str, Any], ready: asyncio.Event) -> None:
        """常驻任务：打开 server 连接、注册工具，保持到收到停止信号。

        连接上下文在本任务进入并在本任务退出，保证 anyio cancel-scope 任务一致性。

        Args:
            name: server 名。
            spec: 该 server 的连接配置。
            ready: 连接就绪（或失败）后置位的事件，供 start() 等待。
        """
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(_CONNECT_TIMEOUT):
                    session = await self._open_session(stack, spec)
                    await session.initialize()
                    listed = await session.list_tools()
                conn = _ServerConn(name=name, session=session)
                for mcp_tool in listed.tools:
                    conn.tool_names.append(self._register_tool(name, session, mcp_tool))
                self._conns[name] = conn
                ready.set()
                await self._stop_event.wait()
        except Exception as exc:
            logger.warning("连接 MCP server '%s' 失败，已跳过：%s", name, exc)
        finally:
            ready.set()

    async def _open_session(self, stack: AsyncExitStack, spec: dict[str, Any]) -> Any:
        """按 transport 打开传输层与 ClientSession，注册到退出栈。

        Args:
            stack: 当前 server 任务的退出栈，进入的上下文由其在同任务统一退出。
            spec: server 连接配置，含 transport 及对应字段。

        Returns:
            未初始化的 ClientSession 实例。
        """
        from mcp import ClientSession, StdioServerParameters

        transport = (spec.get("transport") or "stdio").lower()
        if transport == "stdio":
            from mcp.client.stdio import stdio_client, get_default_environment
            params = StdioServerParameters(
                command=spec["command"],
                args=spec.get("args", []),
                # 默认环境（含 PATH）叠加用户声明的 env，避免子进程丢失 PATH 导致启动失败；
                # env 值先经占位符替换（${workdir}、~），使其能引用随运行变化的绝对路径。
                # 基底过 clean_env：冻结产物里动态库搜索路径被改写过，server 子进程常常
                # 本身就是另一个 Python 程序，继承下去会加载错动态库。
                env=(
                    self.data_guard.safe_environment(
                        clean_env(self.config_mgr.environment),
                        _interpolate_env(spec.get("env") or {}, self.workdir),
                    )
                    if self.data_guard is not None
                    else {
                        **clean_env(get_default_environment()),
                        **_interpolate_env(spec.get("env") or {}, self.workdir),
                    }
                ),
            )
            # 将 server 子进程的 stderr 导向 DEVNULL，避免日志输出到控制台
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=subprocess.DEVNULL)
            )
        elif transport in ("http", "streamable-http", "streamable_http"):
            from mcp.client.streamable_http import streamable_http_client
            import httpx
            http_client = httpx.AsyncClient(headers=spec.get("headers"))
            await stack.enter_async_context(http_client)
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(spec["url"], http_client=http_client)
            )
        elif transport == "sse":
            from mcp.client.sse import sse_client
            read, write = await stack.enter_async_context(
                sse_client(spec["url"], headers=spec.get("headers"))
            )
        else:
            raise ValueError(f"不支持的 MCP transport: {transport}")

        return await stack.enter_async_context(ClientSession(read, write))

    def _register_tool(self, server: str, session: Any, mcp_tool: Any) -> str:
        """将单个 MCP 工具构造为 ToolEntry 并注册进 ToolsMgr。

        Args:
            server: server 名。
            session: 该 server 的 ClientSession。
            mcp_tool: MCP list_tools 返回的 Tool 对象。

        Returns:
            注册使用的工具名（mcp__server__tool，已清洗限长）。
        """
        tool_name = _safe_tool_name(f"mcp__{server}__{mcp_tool.name}")
        schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}
        policy = ToolPolicy(
            AccessKind.REVIEW,
            DataFlow.EXTERNAL,
            detail_template=f"MCP {server}: {mcp_tool.name}",
        )
        upstream_name = mcp_tool.name

        async def _call(**kwargs: Any) -> str:
            result = await session.call_tool(upstream_name, arguments=kwargs)
            return _format_result(result)

        self.tools_mgr.register(ToolEntry(
            name=tool_name,
            func=_call,
            model=_PassThroughArgs,
            description=mcp_tool.description or "",
            parameters_schema=schema,
            policy=policy,
            origin=ToolOrigin("mcp", server),
            subagent=None,
        ))
        return tool_name
