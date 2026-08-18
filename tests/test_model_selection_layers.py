"""模型三轴选择（default 槽位 / fast 槽位 / 角色级推理强度）的跨层契约测试。

覆盖事件层 `EventBus.request_model_selection` 的三元组返回与摘要文案，
以及 `UserInterface._read_model_selection` 非 TTY 三次串行降级路径。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from rich.text import Text

from src.events.bus import EventBus
from src.events.menu import FormQuestion, ModelMenu
from src.interfaces.base import UserInterface

_MODELS = [("model-a", "stub/model-a"), ("model-b", "stub/model-b")]
_EFFORTS = ["low", "medium", "high"]


def _run_request(answer: str) -> tuple[ModelMenu, tuple[str, str, str]]:
    """用后台订阅者以给定 answer 完成一次模型菜单请求。

    Args:
        answer: UI 侧经 future 回传的原始字符串（空串表示取消）。

    Returns:
        (发布出去的 ModelMenu 事件, request_model_selection 的返回三元组)。
    """
    async def scenario() -> tuple[ModelMenu, tuple[str, str, str]]:
        bus = EventBus()
        seen: list[ModelMenu] = []

        async def responder() -> None:
            async for event in bus.subscribe():
                if isinstance(event, ModelMenu):
                    seen.append(event)
                    event.complete(answer)

        task = asyncio.create_task(responder())
        await asyncio.sleep(0)
        result = await bus.request_model_selection(
            "", _MODELS, _EFFORTS, 1, 0, 2, source="models"
        )
        bus.close()
        await task
        return seen[0], result

    return asyncio.run(scenario())


def test_request_model_selection_carries_two_slot_indexes_and_returns_triple() -> None:
    """请求应携带两个槽位下标，并把三键 payload 解成三元组。"""
    payload = json.dumps(
        {"default": "model-b", "fast": "model-a", "reasoning_effort": "high"},
        ensure_ascii=False,
    )

    event, result = _run_request(payload)

    assert event.default_model_index == 1
    assert event.fast_model_index == 0
    assert event.effort_index == 2
    assert result == ("model-b", "model-a", "high")


def test_request_model_selection_cancel_returns_empty_triple() -> None:
    """取消（空串）应返回三个空串。"""
    _event, result = _run_request("")

    assert result == ("", "", "")


def test_interaction_summary_reports_both_slots_and_effort() -> None:
    """摘要应分别给出两个槽位的最终值与推理强度。"""
    event = ModelMenu(timestamp=1.0, source="models")
    answer = json.dumps(
        {"default": "model-b", "fast": "model-a", "reasoning_effort": "high"},
        ensure_ascii=False,
    )

    assert EventBus._interaction_summary(event, answer) == (
        "选择：default=model-b / fast=model-a / 强度=high"
    )


def test_interaction_summary_falls_back_to_raw_answer_on_bad_payload() -> None:
    """payload 不是 JSON 时降级回显原始作答。"""
    event = ModelMenu(timestamp=1.0, source="models")

    assert EventBus._interaction_summary(event, "not-json") == "选择：not-json"


def test_interaction_summary_marks_cancelled_model_menu() -> None:
    """空作答统一走取消文案。"""
    event = ModelMenu(timestamp=1.0, source="models")

    assert EventBus._interaction_summary(event, "") == "[用户取消了作答]"


class _ChoiceOnlyUI(UserInterface):
    """只实现 _read_choice 的最小 UI：按脚本逐次作答并记录每步提示。"""

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def _write(self, message: str | Text, markdown: bool = False) -> None:
        del message, markdown

    async def _read_input(self, prompt: str, default: str = "", markdown: bool = False) -> str:
        del prompt, default, markdown
        return ""

    async def _read_permission(self, tool_name: str, detail: str, reason: str = "") -> str:
        del tool_name, detail, reason
        return "deny"

    async def _read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        markdown: bool = False,
    ) -> str:
        del options, default_index, markdown
        self.prompts.append(prompt)
        return self._answers.pop(0)

    async def _read_form(
        self, prompt: str, questions: list[FormQuestion], markdown: bool = False
    ) -> str:
        del prompt, questions, markdown
        return ""

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        del prompt, options, descriptions, input_placeholder, default_index, markdown
        return ""

    async def _read_transcript_view(self, uuid: str) -> str:
        del uuid
        return ""


def _read_selection(answers: list[str]) -> tuple[_ChoiceOnlyUI, str]:
    """跑一次基类非 TTY 三次串行读取。

    Args:
        answers: 依次作为 default 模型、fast 模型、推理强度的作答。

    Returns:
        (UI 实例, _read_model_selection 返回的原始字符串)。
    """
    ui = _ChoiceOnlyUI(answers)
    payload = asyncio.run(ui._read_model_selection("", _MODELS, _EFFORTS, 0, 1, 2))
    return ui, payload


def test_read_model_selection_asks_three_slots_in_series() -> None:
    """三次串行提示应显式标注槽位，并返回三键 JSON。"""
    ui, payload = _read_selection(["model-b", "model-a", "high"])

    assert len(ui.prompts) == 3
    assert "default 槽位" in ui.prompts[0]
    assert "fast 槽位" in ui.prompts[1]
    assert "推理强度" in ui.prompts[2]
    assert json.loads(payload) == {
        "default": "model-b",
        "fast": "model-a",
        "reasoning_effort": "high",
    }


@pytest.mark.parametrize(
    "answers",
    [
        [""],
        ["model-b", ""],
        ["model-b", "model-a", ""],
    ],
)
def test_read_model_selection_cancels_whole_flow_at_any_step(answers: list[str]) -> None:
    """任一步返回空串即整体取消。"""
    ui, payload = _read_selection(answers)

    assert payload == ""
    assert len(ui.prompts) == len(answers)
