"""PlainFrontend 非 TTY 交互测试：表单推荐后缀展示与模型三轴串行读取。"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from src.events.menu import FormQuestion
from src.interfaces.tui.plain import PlainFrontend


def _run_form(questions: list[FormQuestion], inputs: list[str]):
    stream = io.StringIO()
    remaining = iter(inputs)

    async def reader(_prompt: str) -> str:
        return next(remaining)

    frontend = PlainFrontend(stream=stream, reader=reader)
    payload = asyncio.run(frontend.read_form("🤖 提问", questions))
    return stream.getvalue(), json.loads(payload)


def test_read_form_appends_recommended_suffix_single_choice():
    output, payload = _run_form(
        [FormQuestion(question="模式", options=[("a", "A"), ("b", "B")], recommended=[False, True])],
        ["2", ""],
    )

    assert "2. B(推荐)" in output
    assert "1. A(推荐)" not in output
    assert payload["answers"] == ["b"]


def test_read_form_multi_choice_suffix_and_values():
    output, payload = _run_form(
        [
            FormQuestion(
                question="组件",
                options=[("a", "A"), ("b", "B")],
                multi_select=True,
                recommended=[True, False],
            )
        ],
        ["1,2", ""],
    )

    assert "1. A(推荐)" in output
    assert "2. B(推荐)" not in output
    assert payload["answers"] == ["a、b"]
    assert "(推荐)" not in payload["answers"][0]


def test_read_form_without_recommended_unchanged():
    output, payload = _run_form(
        [FormQuestion(question="模式", options=[("a", "A"), ("b", "B")])],
        ["1", ""],
    )

    assert "(推荐)" not in output
    assert payload["answers"] == ["a"]


def _run_model_selection(inputs: list[str]) -> tuple[str, list[str], str]:
    """跑一次纯文本模型三轴读取。

    Args:
        inputs: 依次作为 default 模型、fast 模型、推理强度的编号输入。

    Returns:
        (屏幕输出, 每步 reader 提示, read_model_selection 返回的原始字符串)。
    """
    stream = io.StringIO()
    remaining = iter(inputs)
    prompts: list[str] = []

    async def reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(remaining)

    frontend = PlainFrontend(stream=stream, reader=reader)
    payload = asyncio.run(
        frontend.read_model_selection(
            "",
            [("model-a", "stub/model-a"), ("model-b", "stub/model-b")],
            ["low", "medium", "high"],
            0,
            1,
            2,
        )
    )
    return stream.getvalue(), prompts, payload


def test_read_model_selection_reads_two_slots_and_effort_in_series():
    """三次串行编号输入应得到三键 JSON，且每步标注所设槽位。"""
    output, prompts, payload = _run_model_selection(["2", "1", "3"])

    assert "设置 default 槽位模型" in output
    assert "设置 fast 槽位模型" in output
    assert "推理强度" in output
    assert len(prompts) == 3
    assert json.loads(payload) == {
        "default": "model-b",
        "fast": "model-a",
        "reasoning_effort": "high",
    }


@pytest.mark.parametrize(
    "inputs",
    [
        ["x"],
        ["2", "x"],
        ["2", "1", "x"],
    ],
)
def test_read_model_selection_cancel_at_any_step_returns_empty(inputs: list[str]):
    """任一步取消（非法输入）即整体返回空串。"""
    _output, prompts, payload = _run_model_selection(inputs)

    assert payload == ""
    assert len(prompts) == len(inputs)
