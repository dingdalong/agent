"""回合计时器的人工等待与并行工作回归测试。"""

from __future__ import annotations

from dataclasses import dataclass

from src.interfaces.turn_clock import TurnClock


@dataclass
class FakeClock:
    """可手动推进的单调时钟。"""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_pure_human_wait_is_fully_paused() -> None:
    """只有人工等待时，整段时间都不计入有效耗时。"""
    source = FakeClock()
    clock = TurnClock(source)

    clock.enter_human_wait()
    source.advance(5.0)
    assert clock.paused_seconds(source()) == 5.0

    clock.exit_human_wait()
    source.advance(2.0)
    assert clock.paused_seconds(source()) == 5.0


def test_work_keeps_clock_running_during_human_wait() -> None:
    """人工等待与后台工作重叠时，只暂停没有工作覆盖的区间。"""
    source = FakeClock()
    clock = TurnClock(source)

    clock.enter_work()
    source.advance(2.0)
    clock.enter_human_wait()
    source.advance(3.0)
    assert clock.paused_seconds(source()) == 0.0

    clock.exit_work()
    source.advance(4.0)
    assert clock.paused_seconds(source()) == 4.0

    clock.enter_work()
    source.advance(3.0)
    assert clock.paused_seconds(source()) == 4.0

    clock.exit_work()
    source.advance(2.0)
    clock.exit_human_wait()
    assert clock.paused_seconds(source()) == 6.0


def test_nested_human_waits_pause_until_the_last_wait_exits() -> None:
    """嵌套人工交互只在最后一个等待结束时恢复计时。"""
    source = FakeClock()
    clock = TurnClock(source)

    clock.enter_human_wait()
    source.advance(2.0)
    clock.enter_human_wait()
    source.advance(3.0)
    clock.exit_human_wait()
    source.advance(2.0)
    assert clock.paused_seconds(source()) == 7.0

    clock.exit_human_wait()
    source.advance(3.0)
    assert clock.paused_seconds(source()) == 7.0


def test_reset_discards_active_pause_and_depths() -> None:
    """回合边界会清除进行中的暂停、累计值和嵌套深度。"""
    source = FakeClock()
    clock = TurnClock(source)

    clock.enter_human_wait()
    clock.enter_human_wait()
    source.advance(4.0)
    clock.reset()

    assert clock.paused_seconds(source()) == 0.0
    source.advance(3.0)
    assert clock.paused_seconds(source()) == 0.0

    clock.enter_human_wait()
    source.advance(2.0)
    assert clock.paused_seconds(source()) == 2.0
