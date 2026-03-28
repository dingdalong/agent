import json
import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP Server connection."""
    name: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    timeout: float = 30.0
    roots: list[str] = field(default_factory=list)  # 客户端声明的根目录列表


def load_mcp_config(path: str) -> list[MCPServerConfig]:
    """Load MCP server configurations from a JSON file.

    Returns an empty list if the file doesn't exist or can't be parsed.
    Skips invalid or disabled server entries with a warning.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.debug(f"MCP 配置文件不存在: {path}，跳过 MCP 初始化")
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"MCP 配置文件读取失败: {path}, {e}")
        return []

    # 顶层 roots 供所有 server 共享
    global_roots: list[str] = data.get("roots", [])

    servers_data = data.get("mcpServers", {})
    configs = []

    for name, server_dict in servers_data.items():
        transport = server_dict.get("transport")
        enabled = server_dict.get("enabled", True)

        if not enabled:
            logger.debug(f"MCP Server '{name}' 已禁用，跳过")
            continue

        if transport not in ("stdio", "http"):
            logger.warning(f"MCP Server '{name}' transport 无效: {transport}，跳过")
            continue

        if transport == "stdio" and not server_dict.get("command"):
            logger.warning(f"MCP Server '{name}' (stdio) 缺少 command，跳过")
            continue

        if transport == "http" and not server_dict.get("url"):
            logger.warning(f"MCP Server '{name}' (http) 缺少 url，跳过")
            continue

        config = MCPServerConfig(
            name=name,
            transport=transport,
            command=server_dict.get("command"),
            args=server_dict.get("args", []),
            env=server_dict.get("env", {}),
            url=server_dict.get("url"),
            enabled=enabled,
            timeout=server_dict.get("timeout", 30.0),
            roots=server_dict.get("roots", global_roots),
        )
        configs.append(config)

    return configs
