"""EventBus — asyncio.Queue 驱动的事件发布订阅。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from rich.text import Text

from src.events.levels import EventLevel
from src.events.types import (
    Event,
    InterruptRequested,
    OutputRequested,
    PermissionNotice,
)
from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    PermissionMenu,
    TranscriptView,
    UiRequest,
)

_SENTINEL = object()
logger = logging.getLogger(__name__)


async def emit_telemetry_safely(
    event_bus: EventBus | None,
    event: Event,
) -> None:
    """发布非关键遥测事件，隔离普通发布故障并保留控制流异常。

    Args:
        event_bus: 接收事件的总线；None 表示无需发布。
        event: 待发布的遥测事件。

    Returns:
        None。

    Raises:
        asyncio.CancelledError: 发布任务被取消时原样传播。
        KeyboardInterrupt: 收到键盘中断时原样传播。
        SystemExit: 进程退出时原样传播。
    """
    if event_bus is None:
        return
    try:
        await event_bus.emit(event)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        logger.warning(
            "遥测事件发布失败 event_type=%s exception_type=%s",
            event.type,
            type(exc).__name__,
        )

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

    投递机制：
    1. reset 拒绝 gate 先取消 UiRequest，避免跨会话入队
    2. 全局级别门控：event.level <= bus.level 才广播
    3. 订阅者类型过滤：每个订阅者可选只关注特定事件类型
    """

    def __init__(self, level: EventLevel = EventLevel.PROGRESS):
        """Initialize an event bus without subscribers or active request gates.

        Args:
            level: Maximum event level delivered to subscribers.

        Returns:
            None.
        """
        self._level = level
        self._subscribers: list[_Subscription] = []
        self._ui_request_rejection_depth = 0
        self._delivery_revision = 0

    async def emit(self, event: Event) -> None:
        """Broadcast one event to matching subscribers without blocking.

        Args:
            event: Event to deliver or reject at the active reset gate.

        Returns:
            None.
        """
        if isinstance(event, UiRequest) and self._ui_request_rejection_depth:
            event.cancel()
            return
        if event.level.value > self._level.value:
            return
        delivered = False
        for sub in self._subscribers:
            if sub.accepts(event):
                sub.queue.put_nowait(event)
                delivered = True
        if delivered:
            self._delivery_revision += 1

    @contextmanager
    def reject_ui_requests(self) -> Iterator[None]:
        """Reject UiRequest events synchronously while a session reset is active.

        Yields:
            None while requests are cancelled instead of enqueued.
        """
        self._ui_request_rejection_depth += 1
        try:
            yield
        finally:
            self._ui_request_rejection_depth -= 1

    async def request_output(self, content: str | Text, source: str = "ui", markdown: bool = False) -> None:
        """通过事件队列请求 UI 串行输出。

        Args:
            content: 要输出的纯文本或 Rich 文本。
            source: 事件来源标识。
            markdown: 为真且 content 为字符串时，UI 按 Markdown 渲染。
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

    async def _request(self, event: UiRequest, kind: str) -> str:
        """构造并发起一次 UI 请求，等待 future 回传结果。

        Args:
            event: 待发布的 UiRequest 子类实例；载荷字段由调用方填好，future 由本方法附加。
            kind: 无订阅者时 NoEventSubscribers 的类别标识（如 "input"/"choice"/"form"/"permission"）。
        Returns:
            UI 经 future 回传的字符串结果。
        """
        if not self._subscribers:
            raise NoEventSubscribers(kind)
        event.future = asyncio.get_running_loop().create_future()
        await self.emit(event)
        return await event.future

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
        return await self._request(
            InputMenu(
                timestamp=time.time(),
                source=source,
                prompt=prompt,
                default=default,
                markdown=markdown,
            ),
            "input",
        )

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
        return await self._request(
            ChoiceMenu(
                timestamp=time.time(),
                source=source,
                prompt=prompt,
                options=options,
                default_index=default_index,
                markdown=markdown,
            ),
            "choice",
        )

    async def request_form(
        self,
        questions: list[FormQuestion],
        prompt: str = "",
        source: str = "ui",
        markdown: bool = False,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> tuple[list[str], str]:
        """通过事件队列请求 UI 以单屏表单读取多个问题的作答与讨论。

        作答与讨论由 UI 侧 JSON 编码进单个 Future[str] 返回，此处再解码，
        复用既有 future 契约，无需拓宽泛型。

        Args:
            questions: 问题列表；每个问题带可选 (value, label) 选项列表（无则自由文本）。
            prompt: 表单上文提示（打印到 scrollback）。
            source: 事件来源标识。
            markdown: 上文提示与问题/选项标签是否按 Markdown 渲染。
            caller_agent_type: 发起本次表单的 agent 类型（主 agent 为「main」），供 UI 标注是哪个 agent 提问。
            caller_uuid: 发起本次表单的 agent 实例 uuid。

        Returns:
            (answers, discussion)：answers 为与 questions 顺序对齐的答案列表，discussion 为讨论栏文本；
            用户取消（Esc）时返回 ([], "")。
        """
        raw = await self._request(
            FormMenu(
                timestamp=time.time(),
                source=source,
                prompt=prompt,
                questions=questions,
                markdown=markdown,
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            ),
            "form",
        )
        if not raw:
            return [], ""
        payload = json.loads(raw)
        return list(payload.get("answers", [])), str(payload.get("discussion", ""))

    async def request_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None = None,
        input_placeholder: str = "",
        default_index: int = 0,
        source: str = "ui",
        markdown: bool = False,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> tuple[str, str]:
        """通过事件队列请求 UI 以「选项列表 + 一行可编辑输入」读取一次作答。

        作答由 UI 侧 JSON 编码进单个 Future[str] 返回，此处再解码为 (choice, text)，
        复用既有 future 契约，无需拓宽泛型。

        Args:
            prompt: 菜单上文提示（打印到 scrollback）。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 等长对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标。
            source: 事件来源标识。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
            caller_agent_type: 发起本次菜单的 agent 类型（主 agent 为「main」），供 UI 标注是哪个 agent 请求。
            caller_uuid: 发起本次菜单的 agent 实例 uuid。

        Returns:
            (choice, text)：选项行提交时 choice=所选 value、text=""；输入行提交时 choice=""、
            text=输入文本；用户取消（Esc）时返回 ("", "")。
        """
        raw = await self._request(
            ChoiceInputMenu(
                timestamp=time.time(),
                source=source,
                prompt=prompt,
                options=options,
                descriptions=descriptions,
                input_placeholder=input_placeholder,
                default_index=default_index,
                markdown=markdown,
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            ),
            "choice_input",
        )
        if not raw:
            return "", ""
        payload = json.loads(raw)
        return str(payload.get("choice", "")), str(payload.get("text", ""))

    async def request_transcript_view(self, uuid: str, source: str = "ui") -> str:
        """通过事件队列请求 UI 以只读分页面板查看某子 agent 的完整原始消息记录。

        Args:
            uuid: 目标子 agent 的 uuid 字符串。
            source: 事件来源标识。

        Returns:
            恒为空串（只读查看；用户 Esc 关闭）。
        """
        return await self._request(
            TranscriptView(
                timestamp=time.time(),
                source=source,
                uuid=uuid,
            ),
            "transcript",
        )

    async def notify_permission(
        self,
        status: Literal["allow", "deny"],
        tool_name: str,
        detail: str = "",
        decision_source: str = "",
        source: str = "permission",
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> None:
        """通过事件队列发布工具权限状态通知。

        Args:
            status: 权限决策结果（allow 静默 / deny）。
            tool_name: 工具名。
            detail: 通知详情文本。
            decision_source: AuthorizationResult.source，UI 据此显示真实裁决来源；
                与下方 source（事件总线来源标识）不是一回事。
            source: 事件来源标识。
            caller_agent_type: 发起该工具调用的 agent 类型（主 agent 为「main」），供 UI 标注是哪个 agent。
            caller_uuid: 发起该工具调用的 agent 实例 uuid。
        """
        await self.emit(PermissionNotice(
            timestamp=time.time(),
            source=source,
            status=status,
            tool_name=tool_name,
            detail=detail,
            decision_source=decision_source,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def request_permission(
        self,
        tool_name: str,
        detail: str,
        reason: str = "",
        source: str = "permission",
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> str:
        """通过事件队列请求 UI 读取工具权限确认。

        Args:
            tool_name: 工具名。
            detail: 权限请求的详细说明。
            reason: 智能权限/预检拿不准的理由，弹窗前在输出区提示给用户。
            source: 事件来源标识。
            caller_agent_type: 发起该工具调用的 agent 类型（主 agent 为「main」），供 UI 标注是哪个 agent 请求授权。
            caller_uuid: 发起该工具调用的 agent 实例 uuid。

        Returns:
            用户的一次性确认结果（yes/deny）。
        """
        return await self._request(
            PermissionMenu(
                timestamp=time.time(),
                source=source,
                tool_name=tool_name,
                detail=detail,
                reason=reason,
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            ),
            "permission",
        )

    async def subscribe(
        self,
        event_types: set[type[Event]] | None = None,
    ) -> AsyncIterator[Event]:
        """Subscribe to matching events until the bus closes the iterator.

        Args:
            event_types: Exact event classes to receive, or None for every event.

        Yields:
            Events delivered to this subscription in FIFO order.
        """
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
        """Wait for subscriber work and one stable event-loop delivery checkpoint.

        Emit calls that enqueue before or during the checkpoint are included. Producers
        that have not begun emitting when the stable checkpoint returns are future work.

        Returns:
            None.
        """
        while True:
            revision = self._delivery_revision
            subscribers = tuple(self._subscribers)
            if subscribers:
                await asyncio.gather(*(sub.queue.join() for sub in subscribers))
            await asyncio.sleep(0)
            if revision == self._delivery_revision:
                return

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
