"""Tools 模块 — 分层架构的工具系统。

对外导出所有公共接口，不包含业务逻辑。
"""

from .schemas import ToolDict
from .registry import ToolEntry, ToolRegistry
from .decorator import tool, get_registry
from .discovery import discover_tools
from .executor import ToolExecutor
from .middleware import (
    Middleware,
    NextFn,
    build_pipeline,
    error_handler_middleware,
    sensitive_confirm_middleware,
    truncate_middleware,
)
from .router import LocalToolProvider, ToolProvider, ToolRouter
from .tool_call import execute_tool_calls

__all__ = [
    "ToolDict",
    "ToolEntry",
    "ToolRegistry",
    "tool",
    "get_registry",
    "discover_tools",
    "ToolExecutor",
    "Middleware",
    "NextFn",
    "build_pipeline",
    "error_handler_middleware",
    "sensitive_confirm_middleware",
    "truncate_middleware",
    "LocalToolProvider",
    "ToolProvider",
    "ToolRouter",
    "execute_tool_calls",
]
