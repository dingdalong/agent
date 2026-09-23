"""提醒注入管理器 — 收集待在下一次 chat 前发送的框架提醒。

通过 register() 注册提醒源（当前唯一注册者是 Agent 装配的 TaskManager），
提醒源只返回框架自身的固定指令，不得返回用户、工具或外部读取内容。ReminderMgr
只排队纯文本；消息角色和历史追加由 Agent 在 chat 前统一处理。

提醒源通过 duck typing 识别：
- get_turn_start_reminder(mode, is_subagent) -> str: 返回纯文本内容
- notify_tool_round(tool_names) -> None
- pop_post_round_reminder(mode, is_subagent) -> str | None: 返回纯文本内容
提醒源只需实现所需的方法，未实现的方法会被跳过。

节流由本中介统一负责（间隔见构造参数）：提醒源只需声明"何时该提醒"，不必自行
实现冷却计数；同一文本提醒在间隔内重复产出会被丢弃。
"""

from __future__ import annotations

from typing import Any

from src.mode import RunMode


class ReminderMgr:
    """集中管理 agent 运行循环中的提醒注入。

    同一文本提醒按工具轮节流：记录每条提醒上次注入的轮次，只有距上次注入达到
    ``interval_rounds`` 且当前不在待发队列时才再次排队。节流放在中介层，避免每个
    提醒源各自实现冷却逻辑；``interval_rounds`` 为 0 时退化为"只去重待发队列"。

    Attributes:
        _providers: 已注册的提醒源列表，按注册顺序迭代。
        _pending: 待在下一次 chat 前发送的提醒文本。
        _interval_rounds: 同一文本两次注入之间的最小工具轮间隔；0 表示不节流。
        _round: 已完成的工具轮计数。
        _last_queued_round: 每条提醒文本上次被排队的轮次。
    """

    def __init__(self, interval_rounds: int = 5) -> None:
        """初始化中介。

        Args:
            interval_rounds: 同一文本提醒两次注入之间的最小工具轮间隔；0 表示不节流。
        """
        self._providers: list[Any] = []
        self._pending: list[str] = []
        self._interval_rounds = max(0, int(interval_rounds))
        self._round = 0
        self._last_queued_round: dict[str, int] = {}

    def register(self, provider: Any) -> None:
        """注册提醒源。重复注册同一对象会被忽略。

        Args:
            provider: 实现了至少一个提醒接口方法的对象。
        """
        if provider not in self._providers:
            self._providers.append(provider)

    def queue_turn_start(
        self, mode: RunMode, is_subagent: bool,
    ) -> None:
        """收集 turn-start 框架提醒，等待下一次 chat 前发送。

        在 _on_request_input 和 run() 子智能体路径中调用。

        Args:
            mode: 调用方 agent 当前运行模式。
            is_subagent: 调用方是否为子智能体。

        """
        for p in self._providers:
            fn = getattr(p, "get_turn_start_reminder", None)
            if fn is None:
                continue
            text = fn(mode, is_subagent)
            self._queue(text)

    def notify_tool_round(self, tool_names: list[str]) -> None:
        """通知所有提醒源一轮工具执行已完成。

        在 _on_execute_tools 末尾调用。

        Args:
            tool_names: 本轮调用的工具名列表，供提醒源判断是否重置内部计数。
        """
        for p in self._providers:
            fn = getattr(p, "notify_tool_round", None)
            if fn is not None:
                fn(tool_names)
        self._round += 1

    def queue_post_round(
        self, mode: RunMode, is_subagent: bool,
    ) -> None:
        """收集 post-round 框架提醒，等待下一次 chat 前发送。

        Args:
            mode: 调用方 agent 当前运行模式。
            is_subagent: 调用方是否为子智能体。

        """
        for p in self._providers:
            fn = getattr(p, "pop_post_round_reminder", None)
            if fn is None:
                continue
            text = fn(mode, is_subagent)
            self._queue(text)

    def _queue(self, text: str | None) -> None:
        """排队非空、未重复且已过节流间隔的框架提醒。"""
        if not text or text in self._pending:
            return
        last_round = self._last_queued_round.get(text)
        if (
            self._interval_rounds > 0
            and last_round is not None
            and self._round - last_round < self._interval_rounds
        ):
            return
        self._pending.append(text)
        self._last_queued_round[text] = self._round

    def pop_pending(self) -> list[str]:
        """取出并清空下一次 chat 的全部框架提醒。"""
        pending = self._pending
        self._pending = []
        return pending
