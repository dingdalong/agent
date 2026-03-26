"""ToolExecutor — 纯工具执行器，只做参数校验和函数调用。"""

import asyncio
import inspect

from pydantic import ValidationError

from .registry import ToolRegistry


class ToolExecutor:
    """校验参数并调用工具函数。

    不包含路由、敏感确认、截断等逻辑，这些由中间件处理。
    """

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """校验参数并执行工具函数。

        Raises:
            ValueError: 工具未注册或参数校验失败
        """
        entry = self.registry.get(tool_name)
        if not entry:
            raise ValueError(f"未注册的工具: {tool_name}")

        try:
            validated = entry.model(**arguments).model_dump()
        except ValidationError as e:
            messages = []
            for err in e.errors()[:3]:
                loc = ".".join(str(x) for x in err["loc"])
                messages.append(f"{loc}: {err['msg']}")
            raise ValueError(f"参数验证失败: {'; '.join(messages)}") from e

        return await self._run_func(entry.func, validated)

    async def _run_func(self, func, kwargs: dict) -> str:
        if asyncio.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            result = await asyncio.to_thread(func, **kwargs)
        return str(result)
