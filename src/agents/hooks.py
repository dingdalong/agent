"""AgentHooks — Agent 级生命周期钩子。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional


class AgentHooks:
    """Agent 级钩子。

    所有钩子均为可选，未设置时调用为 no-op。
    """

    def __init__(
        self,
        on_start: Optional[Callable[..., Awaitable[None]]] = None,
        on_end: Optional[Callable[..., Awaitable[None]]] = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
        on_handoff: Optional[Callable[..., Awaitable[None]]] = None,
        on_error: Optional[Callable[..., Awaitable[None]]] = None,
    ):
        self._on_start = on_start
        self._on_end = on_end
        self._on_tool_call = on_tool_call
        self._on_handoff = on_handoff
        self._on_error = on_error

    async def on_start(self, agent: Any, context: Any) -> None:
        if self._on_start:
            await self._on_start(agent, context)

    async def on_end(self, agent: Any, context: Any, result: Any) -> None:
        if self._on_end:
            await self._on_end(agent, context, result)

    async def on_tool_call(self, agent: Any, context: Any, tool_name: str, args: dict) -> None:
        if self._on_tool_call:
            await self._on_tool_call(agent, context, tool_name, args)

    async def on_handoff(self, agent: Any, context: Any, handoff: Any) -> None:
        if self._on_handoff:
            await self._on_handoff(agent, context, handoff)

    async def on_error(self, agent: Any, context: Any, error: Exception) -> None:
        if self._on_error:
            await self._on_error(agent, context, error)
