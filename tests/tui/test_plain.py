"""PlainFrontend 表单的推荐选项展示测试：显示带 (推荐) 后缀，返回值保持原 value。"""

from __future__ import annotations

import asyncio
import io
import json

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
