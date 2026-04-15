"""CLIInterface — 命令行交互实现。"""

import asyncio

from src.events.types import (
    Event,
    ErrorOccurred,
    ResponseDelta,
    ThinkingDelta,
)

class CLIInterface:
    """基于标准输入/输出的 CLI 交互实现。"""

    def __init__(self) -> None:
        self._in_thinking = False  # 是否正在输出思考流
        self._in_response = False  # 是否正在输出回应流
        self._ask_lock = asyncio.Lock()

    async def input(self, message: str) -> str:
        return await asyncio.to_thread(input, message)

    async def output(self, message: str) -> None:
        print(message, end="", flush=True)

    def _end_thinking_if_needed(self) -> None:
        """如果正在输出思考流，先换行结束。"""
        if self._in_thinking:
            print(flush=True)
            self._in_thinking = False

    def _end_response_if_needed(self) -> None:
        """如果正在输出回应流，先换行结束。"""
        if self._in_response:
            print(flush=True)
            self._in_response = False

    async def on_event(self, event: Event) -> None:
        """按事件类型格式化输出到终端。"""
        # 非 ThinkingDelta 事件到来时，结束思考流
        if not isinstance(event, ThinkingDelta):
            self._end_thinking_if_needed()
        # 非 ResponseDelta 事件到来时，结束回应流
        if not isinstance(event, ResponseDelta):
            self._end_response_if_needed()

        match event:
            case ErrorOccurred(source=name, error=err):
                print(f"  ✗ {name} 错误: {err}", flush=True)
            case ResponseDelta(content=c):
                if not self._in_response:
                    # 回应块开头：打印前缀
                    print("\n助手：", end="", flush=True)
                    self._in_response = True
                print(c, end="", flush=True)
            case ThinkingDelta(content=c):
                if not self._in_thinking:
                    # 思考块开头：打印前缀
                    print("\n💭 ", end="", flush=True)
                    self._in_thinking = True
                print(c, end="", flush=True)

    async def ask(self, question: str) -> str:
        async with self._ask_lock:
            await self.output(f"\n🤖提问: {question}")
            return await self.input("\n你的回答: ")
