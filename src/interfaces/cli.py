"""CLIInterface — 命令行交互实现。"""

import asyncio

from src.interfaces.base import UserInterface


class CLIInterface:
    """基于标准输入/输出的 CLI 交互实现。"""

    async def prompt(self, message: str) -> str:
        return await asyncio.to_thread(input, message)

    async def display(self, message: str) -> None:
        print(message, end="", flush=True)

    async def confirm(self, message: str) -> bool:
        response = await self.prompt(f"{message} (y/n): ")
        return response.strip().lower() in ("y", "yes", "确认")
