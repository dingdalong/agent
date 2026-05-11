"""UserInterface 协议 — 抽象所有用户交互操作。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.events import Event

@runtime_checkable
class UserInterface(Protocol):
    """I/O 抽象协议，CLI 和 Web 各自实现。"""

    async def on_event(self, event: Event) -> None:
        """处理 EventBus 事件，各终端实现自己的展示逻辑。"""
        ...
