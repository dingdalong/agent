"""ask_user 工具的推荐选项契约测试：稳定排序、平行数组对齐与返回格式。"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from src.tools.builtin.ask_user import ask_user


class _FakeEventBus:
    """捕获 request_form 参数并返回预设作答。"""

    def __init__(self, answers: list[str], discussion: str = "") -> None:
        self.answers = answers
        self.discussion = discussion
        self.form_questions = None
        self.kwargs: dict = {}

    async def request_form(self, form_questions, **kwargs):
        self.form_questions = form_questions
        self.kwargs = kwargs
        return self.answers, self.discussion


def _run(questions: list[dict], answers: list[str], discussion: str = ""):
    bus = _FakeEventBus(answers, discussion)
    deps = SimpleNamespace(event_bus=bus)
    agent = SimpleNamespace(agent_type="tester", uuid=uuid.uuid4())
    result = asyncio.run(ask_user(questions=questions, deps=deps, agent=agent))
    return result, bus


def test_recommended_options_sorted_stably_and_arrays_aligned():
    questions = [
        {
            "question": "选哪个？",
            "header": "选择",
            "options": [
                {"label": "B", "description": "db", "preview": "pb", "recommended": False},
                {"label": "A", "description": "da", "preview": "pa", "recommended": True},
                {"label": "C", "description": "dc", "preview": "pc"},
            ],
        }
    ]
    _result, bus = _run(questions, ["A"])

    question = bus.form_questions[0]
    assert question.options == [("A", "A"), ("B", "B"), ("C", "C")]
    assert question.recommended == [True, False, False]
    assert question.descriptions == ["da", "db", "dc"]
    assert question.previews == ["pa", "pb", "pc"]
    assert bus.kwargs["markdown"] is True
    assert bus.kwargs["prompt"] == "🤖 **提问**"
    assert bus.kwargs["caller_agent_type"] == "tester"
    assert bus.kwargs["caller_uuid"]


def test_multiple_recommended_allowed_in_single_and_multi_select():
    questions = [
        {
            "question": "多选题",
            "header": "多选",
            "multi_select": True,
            "options": [
                {"label": "x", "recommended": True},
                {"label": "y", "recommended": True},
                {"label": "z"},
            ],
        },
        {
            "question": "单选题",
            "header": "单选",
            "options": [
                {"label": "p"},
                {"label": "q", "recommended": True},
                {"label": "r", "recommended": True},
            ],
        },
    ]
    _result, bus = _run(questions, ["x", "q"])

    multi, single = bus.form_questions
    assert [label for _value, label in multi.options] == ["x", "y", "z"]
    assert multi.recommended == [True, True, False]
    assert multi.multi_select is True
    assert [label for _value, label in single.options] == ["q", "r", "p"]
    assert single.recommended == [True, True, False]


def test_recommended_defaults_false_and_order_preserved():
    questions = [
        {
            "question": "顺序",
            "header": "顺序",
            "options": [{"label": "甲"}, {"label": "乙"}, {"label": "丙"}],
        }
    ]
    _result, bus = _run(questions, ["甲"])

    question = bus.form_questions[0]
    assert [label for _value, label in question.options] == ["甲", "乙", "丙"]
    assert question.recommended == [False, False, False]


def test_question_without_options_has_none_arrays():
    questions = [{"question": "自由作答", "header": "自由"}]
    _result, bus = _run(questions, ["文本"])

    question = bus.form_questions[0]
    assert question.options is None
    assert question.descriptions is None
    assert question.previews is None
    assert question.recommended is None


def test_cancel_returns_sentinel():
    questions = [{"question": "选哪个？", "header": "选择", "options": [{"label": "A"}]}]
    result, _bus = _run(questions, [])

    assert result == "[用户取消了作答，未回答任何问题]"


def test_return_format_pairs_answers_and_discussion():
    questions = [
        {"question": "第一题", "header": "一", "options": [{"label": "A"}]},
        {"question": "第二题", "header": "二"},
    ]
    result, _bus = _run(questions, ["A", ""], "补充说明")

    assert "问题1：第一题\n回答：A" in result
    assert "问题2：第二题\n回答：[未作答]" in result
    assert result.endswith("讨论：补充说明")
