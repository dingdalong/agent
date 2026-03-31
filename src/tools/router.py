"""ToolProvider 协议 + ToolRouter 统一路由 + LocalToolProvider。"""

import logging
from typing import Protocol, runtime_checkable

from .executor import ToolExecutor
from .middleware import Middleware, NextFn, build_pipeline
from .registry import ToolRegistry
from .schemas import ToolDict

logger = logging.getLogger(__name__)


@runtime_checkable
class ToolProvider(Protocol):
    """工具来源的统一接口。

    ToolRouter 通过此协议查询和执行工具。每个 provider 管理一组工具，
    通过 can_handle 判断是否能处理某个工具名，通过 get_schemas 暴露
    工具的 JSON Schema 供 LLM 选择。

    实现者：LocalToolProvider（本地 @tool）、MCPToolProvider（MCP）、SkillToolProvider（技能）。
    """

    def can_handle(self, tool_name: str) -> bool: ...
    async def execute(self, tool_name: str, arguments: dict) -> str: ...
    def get_schemas(self) -> list[ToolDict]: ...


class ToolRouter:
    """按注册顺序查询 provider，找到第一个能处理的执行。"""

    def __init__(self):
        self._providers: list[ToolProvider] = []

    def add_provider(self, provider: ToolProvider) -> None:
        self._providers.append(provider)

    async def route(self, tool_name: str, arguments: dict) -> str:
        for provider in self._providers:
            if provider.can_handle(tool_name):
                return await provider.execute(tool_name, arguments)
        return f"错误：未找到工具 '{tool_name}'"

    async def ensure_tools(self, tool_names: list[str]) -> None:
        """通知各 provider 预加载指定工具（如按需连接 MCP server）。"""
        for provider in self._providers:
            if hasattr(provider, "ensure_tools"):
                await provider.ensure_tools(tool_names)

    def get_all_schemas(self) -> list[ToolDict]:
        schemas: list[ToolDict] = []
        for provider in self._providers:
            schemas.extend(provider.get_schemas())
        return schemas

    def set_delegate_depth(self, depth: int) -> None:
        """同步 delegate 深度到 DelegateToolProvider。"""
        for provider in self._providers:
            if hasattr(provider, "set_delegate_depth"):
                provider.set_delegate_depth(depth)

    def is_sensitive(self, tool_name: str) -> bool:
        for provider in self._providers:
            if provider.can_handle(tool_name):
                if hasattr(provider, "is_sensitive"):
                    return provider.is_sensitive(tool_name)
                return False
        return False


class LocalToolProvider:
    """本地工具的 Provider 实现，桥接 ToolExecutor + 中间件。"""

    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        middlewares: list[Middleware],
    ):
        self.registry = registry
        self._pipeline: NextFn = build_pipeline(executor.execute, middlewares)

    def can_handle(self, tool_name: str) -> bool:
        return self.registry.has(tool_name)

    async def execute(self, tool_name: str, arguments: dict) -> str:
        return await self._pipeline(tool_name, arguments)

    def get_schemas(self) -> list[ToolDict]:
        return self.registry.get_schemas()

    def is_sensitive(self, tool_name: str) -> bool:
        entry = self.registry.get(tool_name)
        return entry.sensitive if entry else False
