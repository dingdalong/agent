"""EventBus — asyncio.Queue 驱动的事件发布订阅。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from src.events.levels import EventLevel
from src.events.types import (
    ChoiceRequested,
    Event,
    InputRequested,
    InterruptRequested,
    OutputRequested,
    PermissionNotice,
    PermissionRequested,
)

_SENTINEL = object()

class NoEventSubscribers(RuntimeError):
    """需要 UI 响应的事件没有任何订阅者。"""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"cannot request {kind}: no subscribers")


@dataclass
class _Subscription:
    """内部订阅记录。"""

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    event_types: set[type[Event]] | None = None

    def accepts(self, event: Event) -> bool:
        if self.event_types is None:
            return True
        return type(event) in self.event_types


class EventBus:
    """事件总线 — 生产者 emit，消费者 subscribe。

    过滤机制（两层）：
    1. 全局级别门控：event.level <= bus.level 才广播
    2. 订阅者类型过滤：每个订阅者可选只关注特定事件类型
    """

    def __init__(self, level: EventLevel = EventLevel.PROGRESS):
        self._level = level
        self._subscribers: list[_Subscription] = []

    async def emit(self, event: Event) -> None:
        """广播事件到所有匹配的订阅者（非阻塞）。"""
        if event.level.value > self._level.value:
            return
        for sub in self._subscribers:
            if sub.accepts(event):
                sub.queue.put_nowait(event)

    async def request_output(self, content: str, source: str = "ui", markdown: bool = False) -> None:
        """通过事件队列请求 UI 串行输出。

        Args:
            content: 要输出的文本。
            source: 事件来源标识。
            markdown: 为真时 UI 按 Markdown 渲染（用于计划内容、hook 说明等消息型内容）。
        """
        await self.emit(OutputRequested(
            timestamp=time.time(),
            source=source,
            content=content,
            markdown=markdown,
        ))

    async def request_interrupt(self, source: str = "ui") -> None:
        """通过事件队列请求中断当前用户交互或 agent 工作。"""
        await self.emit(InterruptRequested(
            timestamp=time.time(),
            source=source,
        ))

    async def request_input(
        self, prompt: str, source: str = "ui", default: str = "", markdown: bool = False
    ) -> str:
        """通过事件队列请求 UI 串行输入。

        Args:
            prompt: 上文提示（末行为输入标签，由输入框前缀代替）。
            source: 事件来源标识。
            default: 预填默认值。
            markdown: 上文提示是否按 Markdown 渲染。
        Returns:
            用户提交的文本。
        """
        if not self._subscribers:
            raise NoEventSubscribers("input")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self.emit(InputRequested(
            timestamp=time.time(),
            source=source,
            prompt=prompt,
            default=default,
            markdown=markdown,
            future=future,
        ))
        return await future

    async def request_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int = 0,
        source: str = "ui",
        markdown: bool = False,
    ) -> str:
        """通过事件队列请求 UI 以菜单读取一次选择。

        Args:
            prompt: 菜单上文提示（打印到 scrollback）。
            options: 选项列表，每项为 (value, label)；返回所选项的 value。
            default_index: 初始选中项下标。
            source: 事件来源标识。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。

        Returns:
            用户所选项的 value；空串表示用户取消（Esc）。
        """
        if not self._subscribers:
            raise NoEventSubscribers("choice")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self.emit(ChoiceRequested(
            timestamp=time.time(),
            source=source,
            prompt=prompt,
            options=options,
            default_index=default_index,
            markdown=markdown,
            future=future,
        ))
        return await future

    async def notify_permission(
        self,
        status: Literal["allow", "deny", "auto_allow"],
        tool_name: str,
        detail: str = "",
        source: str = "permission",
    ) -> None:
        """通过事件队列发布工具权限状态通知。"""
        await self.emit(PermissionNotice(
            timestamp=time.time(),
            source=source,
            status=status,
            tool_name=tool_name,
            detail=detail,
        ))

    async def request_permission(
        self,
        tool_name: str,
        detail: str,
        source: str = "permission",
        suggested_rules: list[str] | None = None,
    ) -> str:
        """通过事件队列请求 UI 读取工具权限确认。

        Args:
            tool_name: 工具名。
            detail: 权限请求的详细说明。
            source: 事件来源标识。
            suggested_rules: 建议的 allow 规则列表，供 UI 展示给用户。

        Returns:
            用户的确认结果（yes/session/always/deny）。
        """
        if not self._subscribers:
            raise NoEventSubscribers("permission")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self.emit(PermissionRequested(
            timestamp=time.time(),
            source=source,
            tool_name=tool_name,
            detail=detail,
            suggested_rules=suggested_rules or [],
            future=future,
        ))
        return await future

    async def subscribe(
        self,
        event_types: set[type[Event]] | None = None,
    ) -> AsyncIterator[Event]:
        """返回 async iterator，消费者通过 async for 消费事件。"""
        sub = _Subscription(event_types=event_types)
        self._subscribers.append(sub)
        try:
            while True:
                item = await sub.queue.get()
                try:
                    if item is _SENTINEL:
                        break
                    yield item
                finally:
                    sub.queue.task_done()
        finally:
            self._subscribers.remove(sub)

    async def join(self) -> None:
        """等待当前已投递给订阅者的事件全部处理完成。"""
        subscribers = list(self._subscribers)
        if not subscribers:
            return
        await asyncio.gather(*(sub.queue.join() for sub in subscribers))

    def set_level(self, level: EventLevel) -> None:
        """运行时动态调整级别。"""
        self._level = level

    @property
    def level(self) -> EventLevel:
        return self._level

    def close(self) -> None:
        """关闭总线，通知所有订阅者退出。"""
        for sub in self._subscribers:
            sub.queue.put_nowait(_SENTINEL)
