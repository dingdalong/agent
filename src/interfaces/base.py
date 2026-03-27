"""UserInterface 协议 — 抽象所有用户交互操作。"""

from typing import Protocol, runtime_checkable


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
