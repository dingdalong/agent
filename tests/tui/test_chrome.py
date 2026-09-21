"""TUI chrome（活动区 / 状态行 / 输入状态行 / 分隔线）渲染回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.text import Text

from src.events.menu import FormQuestion
from src.events.types import SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore
import src.interfaces.tui.chrome as tui_chrome_module
from src.interfaces.tui.chrome import rich_text_signature

from tests.tui.support import (
    _app,
    _assert_regions_do_not_overlap,
    _open_form,
)


@pytest.mark.integration
def test_repeated_chrome_render_skips_unchanged_content(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)):
            app._tick_timer.pause()
            monkeypatch.setattr(
                tui_chrome_module,
                "time",
                SimpleNamespace(monotonic=lambda: 10.0),
            )
            app._set_activity("回应中")
            app.chrome.render_activity()
            app.chrome.render_status()

            activity_updates: list[tuple[str, bool]] = []
            status_updates: list[Text] = []
            monkeypatch.setattr(
                app._activity_widget,
                "update",
                lambda content, *, layout=True: activity_updates.append(
                    (str(content), layout)
                ),
            )
            monkeypatch.setattr(
                app._status,
                "update",
                lambda content, *, layout=True: status_updates.append(content),
            )

            for _ in range(6):
                app.chrome.render_activity()
                app.chrome.render_status()
            assert activity_updates == []
            assert status_updates == []

            app._set_activity("思考中")
            app.chrome.render_activity()
            assert len(activity_updates) == 1
            app.get_plan_state = lambda: True
            app.chrome.render_status()
            assert len(status_updates) == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_ask_user_ticks_do_not_repeat_static_updates(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            app._tick_timer.pause()
            app._set_activity("回应中")
            form = await _open_form(
                app,
                pilot,
                [FormQuestion(question="模式", options=[("safe", "安全")])],
            )
            app.refresh_chrome()

            activity_updates: list[str] = []
            status_updates: list[Text] = []
            monkeypatch.setattr(
                app._activity_widget,
                "update",
                lambda content, *, layout=True: activity_updates.append(str(content)),
            )
            monkeypatch.setattr(
                app._status,
                "update",
                lambda content, *, layout=True: status_updates.append(content),
            )

            for _ in range(6):
                app._tick()
            assert activity_updates == []
            assert status_updates == []

            app.get_plan_state = lambda: True
            app._tick()
            assert len(status_updates) == 1

            form.future.cancel()
            await pilot.pause()

    asyncio.run(scenario())


def test_rich_text_signature_includes_base_style() -> None:
    red = Text("same", style="red")
    blue = Text("same", style="blue")

    assert red == blue
    assert rich_text_signature(red) != rich_text_signature(blue)


@pytest.mark.integration
def test_input_status_shows_model_and_effort() -> None:
    async def scenario() -> None:
        app = _app(get_model_info=lambda: ("deepseek-v4-pro", "max"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._input_status.display
            assert app._input_status.render().plain == "deepseek-v4-pro max"
            assert app._input_status.region.height == 1
            _assert_regions_do_not_overlap(app)

    asyncio.run(scenario())


@pytest.mark.integration
def test_input_status_keeps_blank_row_without_provider() -> None:
    """无 provider 时保留空行占位，避免输入区随状态有无而上下跳动。"""

    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._input_status.display
            assert app._input_status.render().plain == ""
            assert app._input_status.region.height == 1
            _assert_regions_do_not_overlap(app)

    asyncio.run(scenario())


@pytest.mark.integration
def test_input_status_hidden_while_viewing_transcript() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.register_foreground("main", "main")
        store.record(SubagentLifecycle(
            timestamp=0.0,
            source="subagent",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="start",
        ))
        app = _app(store, get_model_info=lambda: ("deepseek-v4-pro", "max"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._input_status.display
            await app.coordinator.open_live_transcript("worker-0")
            await pilot.pause()
            assert not app._input_status.display
            app.transcript.close()
            await pilot.pause()
            assert app._input_status.display

    asyncio.run(scenario())
