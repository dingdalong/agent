"""跨层共享的回合计时暂停器。"""

from __future__ import annotations

import time
from collections.abc import Callable


class TurnClock:
    """记录本回合「纯人工等待」时段，供状态栏耗时剔除人工交互时间。

    维护两个计数：`_work_depth`（此刻正在执行、代表实际计算的叶子工具数）与
    `_human_wait_depth`（此刻正在等待人工输入的交互数）。当且仅当
    `_human_wait_depth > 0 且 _work_depth == 0`（整轮只在等人工、无人在算）时暂停累计；
    只要有工具在后台计算，时钟继续走。计数由工具执行层（work）与 UI 交互层（human_wait）
    在同一事件循环上同步增减，渲染层每次重绘读取 `paused_seconds` 扣除暂停时长。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """初始化未开始的时钟。

        Args:
            clock: 返回单调递增秒数的时间源，默认 time.monotonic；测试可注入假时钟。
        """
        self._clock = clock
        self._work_depth = 0
        self._human_wait_depth = 0
        self._paused_total = 0.0
        self._pause_started: float | None = None

    def _should_pause(self) -> bool:
        """返回当前是否应处于暂停态（有人工等待且无叶子工具在计算）。

        Returns:
            应暂停为 True。
        """
        return self._human_wait_depth > 0 and self._work_depth == 0

    def _sync(self) -> None:
        """按当前计数重算暂停状态：应暂停且未暂停则开始一段暂停；不应暂停且在暂停则累加并结束。"""
        should = self._should_pause()
        if should and self._pause_started is None:
            self._pause_started = self._clock()
        elif not should and self._pause_started is not None:
            self._paused_total += self._clock() - self._pause_started
            self._pause_started = None

    def enter_work(self) -> None:
        """一个代表实际计算的叶子工具开始执行。"""
        self._work_depth += 1
        self._sync()

    def exit_work(self) -> None:
        """一个代表实际计算的叶子工具执行结束。"""
        self._work_depth = max(0, self._work_depth - 1)
        self._sync()

    def enter_human_wait(self) -> None:
        """一个人工交互（权限询问/计划确认/表单）开始等待用户输入。"""
        self._human_wait_depth += 1
        self._sync()

    def exit_human_wait(self) -> None:
        """一个人工交互结束等待。"""
        self._human_wait_depth = max(0, self._human_wait_depth - 1)
        self._sync()

    def paused_seconds(self, now: float) -> float:
        """返回本回合累计暂停时长（含正在进行的暂停）。

        Args:
            now: 当前 monotonic 秒，用于计入进行中暂停的已过时长。

        Returns:
            累计暂停秒数。
        """
        ongoing = (now - self._pause_started) if self._pause_started is not None else 0.0
        return self._paused_total + ongoing

    def reset(self) -> None:
        """回合边界清零全部计数与累计（进入输入阶段时调用）。"""
        self._work_depth = 0
        self._human_wait_depth = 0
        self._paused_total = 0.0
        self._pause_started = None
