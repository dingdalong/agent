"""子 agent 状态行展示测试：运行中用 spinner + 任务描述，完成后用 ✔ + 任务描述。"""

from __future__ import annotations

import re

from src.interfaces.agent_view_store import (
    AgentSnapshot,
    ContextUsage,
    TokenUsage,
)
from src.interfaces import status_presenter


def _snapshot(running: bool, activity: str, task: str = "") -> AgentSnapshot:
    """构造用于展示断言的 agent 快照。

    Args:
        running: 是否仍在运行。
        activity: 当前实时活动文案。
        task: 委派时的任务摘要。

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
        task=task,
    )


_METRICS_SUFFIX = "↑32.5k(71%) ↓1.3k · 上下文 9.2k(5%) · 1m7s"
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_present_agent_running_with_task_shows_spinner_and_task() -> None:
    """验证运行中的 agent 显示 spinner 图标 + 任务描述（无状态文字）。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(
        _snapshot(running=True, activity="思考中", task="分析代码结构"),
    ).plain

    # spinner 帧字符 + 空格 + agent_type + 两空格 + 任务描述 + 两空格 + metrics
    assert rendered[0] in _SPINNER_CHARS
    assert f"repository-map  分析代码结构  {_METRICS_SUFFIX}" in rendered
    # 不含短 UUID
    assert "2011235e" not in rendered
    # 不含状态文字
    assert "思考中" not in rendered


def test_present_agent_uses_supplied_time_for_spinner_frame() -> None:
    snapshot = _snapshot(running=True, activity="思考中", task="分析代码结构")

    first = status_presenter.present_agent(snapshot, now=0.01).plain
    second = status_presenter.present_agent(snapshot, now=0.11).plain
    identity = status_presenter.present_agent_identity(
        snapshot,
        show_status=False,
        now=0.11,
    ).plain

    assert first[0] == "⠋"
    assert second[0] == "⠙"
    assert identity[0] == "⠙"


def test_present_agent_completed_with_task_shows_checkmark_and_task() -> None:
    """验证已完成的 agent 显示 ✔ 图标 + 任务描述（无状态文字）。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(
        _snapshot(running=False, activity="思考中", task="分析代码结构"),
    ).plain

    assert rendered == f"✔ repository-map  分析代码结构  {_METRICS_SUFFIX}"


def test_present_agent_running_without_task_falls_back_to_uuid() -> None:
    """验证运行中但无任务描述时回退到短 UUID + 状态文字。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(
        _snapshot(running=True, activity="思考中"),
    ).plain

    assert rendered[0] in _SPINNER_CHARS
    assert "repository-map  2011235e  思考中" in rendered


def test_present_agent_completed_without_task_falls_back_to_uuid() -> None:
    """验证已完成但无任务描述时回退到短 UUID + 状态文字。

    Returns:
        无返回值。
    """
    rendered = status_presenter.present_agent(
        _snapshot(running=False, activity="思考中"),
    ).plain

    assert rendered == f"✔ repository-map  2011235e  已完成  {_METRICS_SUFFIX}"


def test_present_agent_identity_omits_status_slot_for_transcript_header() -> None:
    """验证 show_status=False 时身份行仅含图标/类型/任务描述或短 uuid，不含状态词。

    Returns:
        无返回值。
    """
    # 有任务描述
    snapshot = _snapshot(running=False, activity="思考中", task="分析代码结构")
    assert status_presenter.present_agent_identity(
        snapshot, show_status=False,
    ).plain == "✔ repository-map  分析代码结构"

    # 无任务描述，回退到短 UUID
    snapshot = _snapshot(running=False, activity="思考中")
    assert status_presenter.present_agent_identity(
        snapshot, show_status=False,
    ).plain == "✔ repository-map  2011235e"

    # 运行中
    snapshot = _snapshot(running=True, activity="思考中", task="分析代码结构")
    rendered = status_presenter.present_agent_identity(
        snapshot, show_status=False,
    ).plain
    assert rendered[0] in _SPINNER_CHARS
    assert "repository-map  分析代码结构" in rendered
