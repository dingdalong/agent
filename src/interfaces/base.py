"""UserInterface 协议 — 抽象所有用户交互操作。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.events.types import Event


@runtime_checkable
class UserInterface(Protocol):
    """I/O 抽象协议，CLI 和 Web 各自实现。"""

    async def prompt(self, message: str) -> str:
        """获取用户输入，message 作为提示语。"""
        ...

    async def display(self, message: str) -> None:
        """展示信息给用户。"""
        ...

    async def confirm(self, message: str) -> bool:
        """请求用户确认，返回 True/False。"""
        ...

    async def on_event(self, event: Event) -> None:
        """处理 EventBus 事件，各终端实现自己的展示逻辑。"""
        ...
