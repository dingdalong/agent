"""EventBus — asyncio.Queue 驱动的事件发布订阅。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from src.events.levels import EventLevel
from src.events.types import Event, InputRequested, OutputRequested

_SENTINEL = object()

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

    async def request_output(self, content: str, source: str = "ui") -> None:
        """通过事件队列请求 UI 串行输出。"""
        await self.emit(OutputRequested(
            timestamp=time.time(),
            source=source,
            content=content,
        ))

    async def request_input(self, prompt: str, source: str = "ui") -> str:
        """通过事件队列请求 UI 串行输入。"""
        if not self._subscribers:
            raise RuntimeError("cannot request input: no subscribers")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self.emit(InputRequested(
            timestamp=time.time(),
            source=source,
            prompt=prompt,
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
