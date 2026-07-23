"""子 agent 状态槽展示测试：运行中显示实时活动、完成后显示「已完成」、顶部可隐藏状态词。"""

from __future__ import annotations

from src.interfaces.agent_view_store import (
    AgentSnapshot,
    ContextUsage,
    TokenUsage,
)
from src.interfaces import status_presenter


def _snapshot(running: bool, activity: str) -> AgentSnapshot:
    """构造用于展示断言的 agent 快照。

    Args:
        running: 是否仍在运行。
        activity: 当前实时活动文案。

    Returns:
        填好统一 token/context/耗时的不可变快照。
    """
    return AgentSnapshot(
        uuid="2011235e-rest",
        agent_type="repository-map",
        is_main=False,
        running=running,
        usage=TokenUsage(input_tokens=32_540, output_tokens=1_300, cache_read_tokens=23_103),
        context=ContextUsage(used_tokens=9_200, limit_tokens=200_000),
        elapsed_seconds=67.0,
        activity=activity,
    )


def test_present_agent_running_shows_live_activity_in_status_slot() -> None:
    """验证运行中的 agent 状态槽显示实时活动，且不再追加重复的活动后缀。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(_snapshot(running=True, activity="思考中")).plain

    assert rendered == (
        "◯ repository-map  2011235e  思考中  ↑32.5k(71%) ↓1.3k · 上下文 9.2k(5%) · 1m7s"
    )
    assert not rendered.endswith("· 思考中")


def test_present_agent_running_without_activity_falls_back_to_running_label() -> None:
    """验证运行中但尚无实时活动时状态槽回退「运行中」。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(_snapshot(running=True, activity="")).plain

    assert rendered == (
        "◯ repository-map  2011235e  运行中  ↑32.5k(71%) ↓1.3k · 上下文 9.2k(5%) · 1m7s"
    )


def test_present_agent_completed_shows_done_label_ignoring_stale_activity() -> None:
    """验证已结束的 agent 状态槽显示「已完成」，忽略结束后残留的陈旧活动。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(_snapshot(running=False, activity="思考中")).plain

    assert rendered == (
        "◯ repository-map  2011235e  已完成  ↑32.5k(71%) ↓1.3k · 上下文 9.2k(5%) · 1m7s"
    )


def test_present_agent_identity_omits_status_slot_for_transcript_header() -> None:
    """验证 show_status=False 时身份行仅含标记/类型/短 uuid，不含任何状态词（顶部标题栏用）。

    Returns:
        无返回值。
    """
    for running in (True, False):
        snapshot = _snapshot(running=running, activity="思考中")
        assert status_presenter.present_agent_identity(
            snapshot, show_status=False
        ).plain == "◯ repository-map  2011235e"
