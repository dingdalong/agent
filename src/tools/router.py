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
    """所有工具来源的统一接口"""

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

    def get_all_schemas(self) -> list[ToolDict]:
        schemas: list[ToolDict] = []
        for provider in self._providers:
            schemas.extend(provider.get_schemas())
        return schemas

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
