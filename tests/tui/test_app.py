"""Textual 生产 TUI 的关键交互回归测试。"""

from __future__ import annotations

import asyncio
import gc
import warnings
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual import events
from textual.color import Color
from textual.containers import VerticalScroll
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Markdown, Static, TextArea

from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    ModelMenu,
    PermissionMenu,
)
from src.events.types import (
    PermissionNotice,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
)
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.turn_clock import TurnClock
import src.interfaces.tui.app as tui_app_module
from src.interfaces.tui.app import AgentTuiApp, _rich_text_signature
from src.interfaces.tui.dialogs import (
    InlineFormWidget,
    InlineSelectionWidget,
    SelectionDialog,
)
from src.interfaces.tui.render_policy import TuiRenderPolicy
from src.interfaces.tui.widgets import (
    Composer,
    KeyboardNavigation,
    KeyboardOptionList,
    SelectionScreen,
    SelectionStatic,
)
from src.mgr.session_state import SessionState


def _app(
    store: AgentViewStore | None = None,
    *,
    platform: str = "darwin",
    get_model_info=None,
    render_policy: TuiRenderPolicy | None = None,
) -> AgentTuiApp:
    return AgentTuiApp(
        store or AgentViewStore(),
        [("clear", "清理会话"), ("agents", "查看 Agent")],
        TurnClock(),
        lambda: None,
        lambda: False,
        lambda: None,
        get_model_info=get_model_info,
        platform_name=platform,
        native_clipboard=False,
        render_policy=render_policy,
    )


async def _open_form(app, pilot, questions, **kwargs):
    """提交 FormMenu 并等待挂载，返回请求对象。"""
    loop = asyncio.get_running_loop()
    form = FormMenu(
        timestamp=1.0,
        source="test",
        questions=questions,
        future=loop.create_future(),
        **kwargs,
    )
    await app.coordinator.submit(form)
    await pilot.pause()
    return form


def _assert_regions_do_not_overlap(app: AgentTuiApp) -> None:
    regions = [
        widget.region
        for widget in app.screen.children
        if widget.display and widget.region.height
    ]
    for upper, lower in zip(regions, regions[1:]):
        assert upper.bottom <= lower.y, (upper, lower)
    assert regions[-1].bottom <= app.screen.size.height


async def _wait_for_transcript_render(app: AgentTuiApp, pilot) -> None:
    for _ in range(200):
        if (
            app._transcript_pending is None
            and app._transcript_active_renders == 0
            and app._rendered_transcript_id == app.viewing_agent_id
        ):
            await pilot.pause()
            return
        await pilot.pause(0.01)
    raise AssertionError("transcript render did not become idle")


def test_malformed_completed_transcript_is_safe_and_switchable() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        for uuid in ("worker-0", "worker-1"):
            store.record(SubagentLifecycle(
                timestamp=1.0,
                source="test",
                agent_uuid=uuid,
                agent_type="worker",
                phase="end",
                messages=[
                    {"role": "assistant", "tool_calls": [None, {"function": None}]},
                    "unexpected message",
                ],
            ))
        store.flush_completed()
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.open_transcript("worker-0", ["worker-0", "worker-1"], invoked=False)
            await pilot.pause()
            app.switch_transcript(1)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert app.viewing_agent_id == "worker-1"
            assert "unexpected message" in app._transcript_content.source

    asyncio.run(scenario())


def test_rapid_transcript_switches_coalesce_without_cancelling_render() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        for uuid in ("worker-0", "worker-1"):
            store.record(SubagentLifecycle(
                timestamp=1.0,
                source="test",
                agent_uuid=uuid,
                agent_type="worker",
                phase="end",
                messages=[{"role": "assistant", "content": f"transcript {uuid}"}],
            ))
        store.flush_completed()
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.open_transcript(
                "worker-0",
                ["worker-0", "worker-1"],
                invoked=False,
            )
            for _ in range(50):
                app.switch_transcript(1)
                app.switch_transcript(-1)
            await _wait_for_transcript_render(app, pilot)
            assert app.viewing_agent_id == "worker-0"
            assert app._rendered_transcript_id == "worker-0"
            assert app._transcript_max_concurrent_renders == 1
            assert app._transcript_merged_requests > 0
            assert app.is_running

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        asyncio.run(scenario())
        gc.collect()
    assert not [
        warning
        for warning in captured
        if "coroutine" in str(warning.message)
        and "was never awaited" in str(warning.message)
    ]


def test_transcript_tick_does_not_overwrite_position_during_render(monkeypatch) -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        content = "\n\n".join(f"paragraph {index}" for index in range(50))
        for uuid in ("worker-0", "worker-1"):
            store.record(SubagentLifecycle(
                timestamp=1.0,
                source="test",
                agent_uuid=uuid,
                agent_type="worker",
                phase="start",
            ))
            store.record(ResponseDelta(
                timestamp=1.0,
                source="test",
                caller_uuid=uuid,
                caller_agent_type="worker",
                content=content,
            ))

        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.open_transcript(
                "worker-0",
                ["worker-0", "worker-1"],
                invoked=False,
            )
            await _wait_for_transcript_render(app, pilot)
            app._transcript_panel.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            app._save_transcript_position()
            assert app._transcript_positions["worker-0"].scroll_y == 0

            app.switch_transcript(1)
            await _wait_for_transcript_render(app, pilot)
            assert app._transcript_panel.scroll_y > 0

            wait_for_refresh = app._transcript_panel.wait_for_refresh

            async def wait_for_refresh_after_tick() -> None:
                app._tick()
                await wait_for_refresh()

            monkeypatch.setattr(
                app._transcript_panel,
                "wait_for_refresh",
                wait_for_refresh_after_tick,
            )
            app.switch_transcript(-1)
            await _wait_for_transcript_render(app, pilot)

            assert app._transcript_positions["worker-0"].scroll_y == 0
            assert app._transcript_panel.scroll_y == 0

    asyncio.run(scenario())


def test_large_transcript_switching_stays_responsive_during_response_stream() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        large = "\n\n".join(
            f"## section {index}\n\n```python\nvalue_{index} = {index}\n```"
            for index in range(80)
        )
        for uuid in ("worker-0", "worker-1"):
            store.record(SubagentLifecycle(
                timestamp=1.0,
                source="test",
                agent_uuid=uuid,
                agent_type="worker",
                phase="end",
                messages=[{"role": "assistant", "content": f"{uuid}\n\n{large}"}],
            ))
        store.flush_completed()
        app = _app(store)
        event = ResponseDelta(
            timestamp=2.0,
            source="test",
            caller_uuid="main",
            caller_agent_type="main",
            content="",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await app.open_transcript(
                "worker-0",
                ["worker-0", "worker-1"],
                invoked=False,
            )

            async def stream_response() -> None:
                for index in range(60):
                    await app.on_response_delta(event, f"stream {index}\n\n")
                    await asyncio.sleep(0)

            async def switch_agents() -> None:
                for _ in range(30):
                    app.switch_transcript(1)
                    await asyncio.sleep(0)
                    app.switch_transcript(-1)
                    await asyncio.sleep(0)

            await asyncio.wait_for(
                asyncio.gather(stream_response(), switch_agents()),
                timeout=8,
            )
            await _wait_for_transcript_render(app, pilot)
            assert app.viewing_agent_id == "worker-0"
            assert app._rendered_transcript_id == "worker-0"
            assert app._transcript_max_concurrent_renders == 1
            assert app._transcript_worker_task is not None
            assert not app._transcript_worker_task.done()
            assert app.is_running

    asyncio.run(scenario())


def test_mouse_down_ignores_detached_markdown_hit(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 30)) as pilot:
            markdown = Markdown("old paragraph")
            await app._history.mount(markdown)
            await pilot.pause()
            detached = markdown.query_one("MarkdownParagraph")

            await markdown.update("replacement paragraph")
            await pilot.pause()
            assert not detached.is_attached

            monkeypatch.setattr(
                app.screen,
                "get_widget_and_offset_at",
                lambda _x, _y: (detached, Offset(0, 0)),
            )
            region = app._history.content_region
            app.screen._forward_event(events.MouseDown(
                None,
                region.x,
                region.y,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=region.x,
                screen_y=region.y,
            ))

            assert app.screen._select_state is None
            assert app.is_running

    asyncio.run(scenario())


def test_completed_agents_do_not_steal_focus_from_open_transcript() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="test",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="start",
        ))
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.coordinator.open_live_transcript("worker-0")
            await pilot.pause()
            store.record(SubagentLifecycle(
                timestamp=2.0,
                source="test",
                agent_uuid="worker-0",
                agent_type="worker",
                phase="end",
            ))
            store.flush_completed()
            app._schedule_agent_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.viewing_agent_id == "worker-0"
            assert app._transcript_panel.has_focus

    asyncio.run(scenario())


def test_stale_transcript_ids_are_safe() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="test",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="end",
        ))
        store.flush_completed()
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.open_transcript("worker-0", ["missing"], invoked=False)
            app._transcript_ids = ["missing"]
            app.switch_transcript(1)
            await pilot.pause()
            assert app.viewing_agent_id is None
            assert app.is_running

    asyncio.run(scenario())


def test_responsive_input_history_and_ctrl_c() -> None:
    async def scenario() -> None:
        app = _app(platform="win32")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _assert_regions_do_not_overlap(app)
            await pilot.resize_terminal(74, 27)
            await pilot.pause()
            assert app.screen.has_class("compact")
            _assert_regions_do_not_overlap(app)

            for index in range(36):
                await app.append_markdown(
                    f"### 历史滚动段落 {index:02d}\n\n用于验证底部跟随。"
                )
            await pilot.pause()
            assert app._history.is_anchored
            assert app._history.scroll_y == app._history.max_scroll_y
            app._history.scroll_to(
                y=max(0, app._history.max_scroll_y - 8),
                animate=False,
                immediate=True,
            )
            old_scroll_y = app._history.scroll_y
            await app.append_markdown("上滚期间追加内容")
            await pilot.pause()
            assert app._history.scroll_y == old_scroll_y
            app._history.scroll_end(animate=False, immediate=True)
            app._history.resume_anchor_at_end()
            await app.append_markdown("回到底部后继续跟随")
            await pilot.pause()
            assert app._history.scroll_y == app._history.max_scroll_y

            future = asyncio.get_running_loop().create_future()
            request = InputMenu(timestamp=1.0, source="test", prompt="输入", future=future)
            assert await app.coordinator.submit(request)
            await pilot.pause()
            assert app._composer_shell.region.height == 1
            assert app._composer.region.height == app._composer_shell.content_region.height
            assert app._composer.has_focus
            app.screen.set_focus(None)
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app._composer.has_focus
            app._composer.load_text("abcd")
            app._composer.move_cursor((0, 4))
            await pilot.click(app._history)
            await pilot.pause()
            assert app._composer.has_focus
            await pilot.click(app._status)
            await pilot.pause()
            assert app._composer.has_focus
            await pilot.click(app._composer, offset=(0, 0))
            await pilot.pause()
            assert app._composer.has_focus
            # 放开鼠标后，点击左上角会把光标定位到行首（不再保持原位）
            assert app._composer.cursor_location == (0, 0)
            app._composer.load_text("\n".join(str(index) for index in range(9)))
            await pilot.pause()
            assert app._composer_shell.styles.height.value == 8
            app._composer.clear()
            await pilot.pause()
            assert app._composer_shell.styles.height.value == 1
            # 软折行（无显式 \n）也应撑高输入栏：视觉行数 = wrapped_document.height
            app._composer.load_text("a" * 500)
            await pilot.pause()
            expected_height = min(8, max(1, app._composer.wrapped_document.height))
            assert expected_height > 1
            assert app._composer_shell.styles.height.value == expected_height
            app._composer.clear()
            await pilot.pause()
            assert app._composer_shell.styles.height.value == 1
            await pilot.press("h", "i", "shift+enter", "x")
            assert app._composer.text == "hi\nx"
            assert app._composer_shell.styles.height.value == 2
            assert app._composer.region.height == 2
            await pilot.press("enter")
            await pilot.pause()
            assert await future == "hi\nx"
            assert app._history.scroll_y == app._history.max_scroll_y

            exit_future = asyncio.get_running_loop().create_future()
            await app.coordinator.submit(InputMenu(
                timestamp=2.0,
                source="test",
                future=exit_future,
            ))
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert exit_future.cancelled()

    asyncio.run(scenario())


def test_render_policy_controls_history_tick_and_focus_pause() -> None:
    async def scenario() -> None:
        policy = TuiRenderPolicy(activity_interval=0.25)
        app = _app(render_policy=policy)
        async with app.run_test(size=(100, 30)) as pilot:
            assert app.render_policy is policy
            assert app.history_journal._policy is policy
            assert app._history._policy is policy
            assert app._history._diagnostics is app.diagnostics
            assert app._tick_timer._interval == policy.activity_interval

            app.post_message(events.AppBlur())
            await pilot.pause()
            assert not app._tick_timer._active.is_set()

            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app._tick_timer._active.is_set()

    asyncio.run(scenario())


def test_keyboard_text_areas_keep_cursor_visible_without_blinking(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            request = InputMenu(
                timestamp=1.0,
                source="test",
                prompt="输入",
                future=asyncio.get_running_loop().create_future(),
            )
            await app.coordinator.submit(request)
            await pilot.pause()

            composer_refreshes: list[tuple[int, int]] = []
            monkeypatch.setattr(
                app._composer,
                "refresh_lines",
                lambda start, count=1: composer_refreshes.append((start, count)),
            )
            assert app._composer.has_focus
            assert app._composer.show_cursor
            assert app._composer._draw_cursor
            assert not app._composer.cursor_blink
            await pilot.pause(0.55)
            assert composer_refreshes == []

            await pilot.press("x", "enter")
            assert await request.future == "x"
            form = await _open_form(
                app,
                pilot,
                [FormQuestion(question="说明")],
            )
            custom_input = app._interaction_slot.query_one(
                "#custom-input-0",
                TextArea,
            )
            form_refreshes: list[tuple[int, int]] = []
            monkeypatch.setattr(
                custom_input,
                "refresh_lines",
                lambda start, count=1: form_refreshes.append((start, count)),
            )
            assert custom_input.has_focus
            assert custom_input.show_cursor
            assert custom_input._draw_cursor
            assert not custom_input.cursor_blink
            await pilot.pause(0.55)
            assert form_refreshes == []

            form.future.cancel()
            await pilot.pause()

    asyncio.run(scenario())


def test_repeated_chrome_render_skips_unchanged_content(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)):
            app._tick_timer.pause()
            monkeypatch.setattr(
                tui_app_module,
                "time",
                SimpleNamespace(monotonic=lambda: 10.0),
            )
            app._set_activity("回应中")
            app._render_activity()
            app._render_status()

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
                app._render_activity()
                app._render_status()
            assert activity_updates == []
            assert status_updates == []

            app._set_activity("思考中")
            app._render_activity()
            assert len(activity_updates) == 1
            app.get_plan_state = lambda: True
            app._render_status()
            assert len(status_updates) == 1

    asyncio.run(scenario())


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
    assert _rich_text_signature(red) != _rich_text_signature(blue)


def test_failed_history_append_is_not_committed(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)):
            def fail_append(*_args, **_kwargs) -> None:
                raise RuntimeError("append failed")

            monkeypatch.setattr(app._history, "append_entry", fail_append)
            with pytest.raises(RuntimeError, match="append failed"):
                await app.append_output("not displayed")

            assert app.history_journal.snapshot() == ""

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "/models",
        "  /MODELS  ",
        "/models ignored-argument",
    ],
)
def test_models_command_input_is_recorded(text: str) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.get_running_loop().create_future()
            request = InputMenu(timestamp=1.0, source="test", future=future)
            await app.coordinator.submit(request)
            await app.coordinator.complete_input(text)
            await pilot.pause()

            assert await future == text
            history = app.history_journal.snapshot()
            assert text in history

    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", [False, True])
def test_models_flow_only_keeps_success_output(cancelled: bool) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)) as pilot:
            loop = asyncio.get_running_loop()
            command = InputMenu(
                timestamp=1.0,
                source="test",
                future=loop.create_future(),
            )
            await app.coordinator.submit(command)
            await app.coordinator.complete_input("/models")
            assert await command.future == "/models"

            selection = ModelMenu(
                timestamp=2.0,
                source="models",
                models=[("model-a", "provider/model-a")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                future=loop.create_future(),
            )
            await app.coordinator.submit(selection)
            await pilot.pause()
            await pilot.press("escape" if cancelled else "enter")
            await selection.future
            await pilot.pause()

            if cancelled:
                assert app.history_journal.snapshot() == "› /models\n"
                return
            await app.append_output("已切换模型：model-a，推理强度：low。")
            assert app.history_journal.snapshot() == (
                "› /models\n已切换模型：model-a，推理强度：low。\n"
            )

    asyncio.run(scenario())


def test_inline_widgets_define_their_own_completion_history() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(90, 28)) as pilot:
            loop = asyncio.get_running_loop()

            choice = ChoiceMenu(
                timestamp=1.0,
                source="test",
                options=[("ok", "继续")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice)
            await pilot.pause()
            assert app._interaction_slot.display
            assert len(app._interaction_slot.children) == 1
            assert not app._history.children
            await pilot.press("escape")
            assert await choice.future == ""
            await pilot.pause()
            assert not app._interaction_slot.display
            assert not app._interaction_slot.children
            assert app.history_journal.snapshot() == ""

            choice_input = ChoiceInputMenu(
                timestamp=2.0,
                source="test",
                options=[("auto", "自动")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice_input)
            await pilot.pause()
            await pilot.press("escape")
            assert await choice_input.future == ""
            await pilot.pause()
            assert app.history_journal.snapshot() == ""

            model = ModelMenu(
                timestamp=3.0,
                source="models",
                models=[("model-a", "provider/model-a")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                future=loop.create_future(),
            )
            await app.coordinator.submit(model)
            await pilot.pause()
            await pilot.press("enter")
            assert await model.future == (
                '{"model": "model-a", "reasoning_effort": "low"}'
            )
            await pilot.pause()
            assert app.history_journal.snapshot() == ""

            cancelled_model = ModelMenu(
                timestamp=4.0,
                source="models",
                models=[("model-a", "provider/model-a")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                future=loop.create_future(),
            )
            await app.coordinator.submit(cancelled_model)
            await pilot.pause()
            await pilot.press("escape")
            assert await cancelled_model.future == ""
            await pilot.pause()
            assert app.history_journal.snapshot() == ""

            form = FormMenu(
                timestamp=5.0,
                source="ask_user",
                questions=[FormQuestion(question="继续？")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(form)
            await pilot.pause()
            await pilot.press("escape")
            assert await form.future == ""
            await pilot.pause()
            assert app.history_journal.snapshot() == "[用户取消了作答]\n"

            permission = PermissionMenu(
                timestamp=6.0,
                source="test",
                tool_name="shell",
                detail="echo test",
                future=loop.create_future(),
            )
            await app.coordinator.submit(permission)
            await pilot.pause()
            before_permission = app.history_journal.snapshot()
            await pilot.press("escape")
            assert await permission.future == "deny"
            await pilot.pause()
            assert app.history_journal.snapshot() == (
                before_permission + "权限确认：shell → 拒绝\n"
            )

            successful_choice = ChoiceMenu(
                timestamp=7.0,
                source="test",
                options=[("ok", "继续")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(successful_choice)
            await pilot.pause()
            before_choice = app.history_journal.snapshot()
            await pilot.press("enter")
            assert await successful_choice.future == "ok"
            await pilot.pause()
            assert app.history_journal.snapshot() == before_choice + "选择：继续\n"

            successful_choice_input = ChoiceInputMenu(
                timestamp=8.0,
                source="test",
                options=[("auto", "自动")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(successful_choice_input)
            await pilot.pause()
            before_choice_input = app.history_journal.snapshot()
            await pilot.press("enter")
            assert await successful_choice_input.future == (
                '{"choice": "auto", "text": ""}'
            )
            await pilot.pause()
            assert app.history_journal.snapshot() == (
                before_choice_input + "选择：自动\n"
            )

            successful_form = FormMenu(
                timestamp=9.0,
                source="ask_user",
                questions=[FormQuestion(
                    question="模式",
                    options=[("safe", "安全")],
                )],
                future=loop.create_future(),
            )
            await app.coordinator.submit(successful_form)
            await pilot.pause()
            before_form = app.history_journal.snapshot()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            assert await successful_form.future == (
                '{"answers": ["safe"], "discussion": ""}'
            )
            await pilot.pause()
            assert app.history_journal.snapshot() == (
                before_form + "用户选择：\n  问题1 → safe\n"
            )

    asyncio.run(scenario())


def test_permission_notice_renders_full_reason_and_wraps() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(60, 20)) as pilot:
            detail = "无法读取路径信息：/Users/x/" + "seg/" * 30
            await app.on_permission_notice(PermissionNotice(
                timestamp=0.0,
                source="permission",
                status="deny",
                tool_name="get_file_info",
                detail=detail,
                decision_source="hard_rule",
            ))
            await pilot.pause()
            entry = app._history.entries[-1]
            rendered = entry.content.plain if isinstance(entry.content, Text) else entry.content
            assert "硬规则" in rendered
            assert detail in rendered
            # 60 列终端下长路径应折行（高度 > 1）而不是被截断。
            await pilot.pause()
            start, end = app._history.entry_ranges[entry.id]
            assert end - start > 2

    asyncio.run(scenario())


def test_keyboard_focus_moves_between_composer_agent_list_and_transcript() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.register_foreground("main", "main")
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="subagent",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="start",
        ))
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            request = InputMenu(
                timestamp=2.0,
                source="test",
                future=asyncio.get_running_loop().create_future(),
            )
            await app.coordinator.submit(request)
            await pilot.pause(0.2)

            assert app._composer.has_focus
            await pilot.press("down")
            assert app._agent_list.has_focus
            assert app._main_focus_target == "agent_list"

            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app._agent_list.has_focus

            await pilot.click(app._agent_list.children[0])
            await pilot.pause()
            assert app._agent_list.has_focus
            assert app.viewing_agent_id is None

            await pilot.press("up")
            assert app._composer.has_focus
            assert app._main_focus_target == "composer"

            await pilot.press("g", "o", "enter")
            await pilot.pause()
            assert await request.future == "go"
            assert not app.coordinator.input_active
            assert app._composer.read_only
            assert app._composer.has_focus
            assert app._composer.show_cursor
            assert app._composer._draw_cursor
            assert app._composer.text == ""

            await pilot.press("x", "shift+enter", "enter")
            await pilot.pause()
            assert app._composer.text == ""
            assert app._composer.has_focus

            await pilot.press("down")
            assert app._agent_list.has_focus
            assert app._main_focus_target == "agent_list"
            assert app._composer.show_cursor
            assert not app._composer._draw_cursor
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app._agent_list.has_focus

            permission = PermissionMenu(
                timestamp=2.5,
                source="test",
                tool_name="shell",
                detail="pwd",
                future=asyncio.get_running_loop().create_future(),
            )
            await app.coordinator.submit(permission)
            await pilot.pause()
            assert not app._agent_list.display
            assert not app._composer.show_cursor
            await pilot.press("1")
            await app.coordinator.wait_idle()
            await pilot.pause()
            assert await permission.future == "yes"
            assert app._agent_list.display
            assert app._agent_list.has_focus

            await pilot.press("up")
            assert app._composer.has_focus
            assert app._composer.read_only
            assert app._composer._draw_cursor

            await pilot.press("down", "down", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.viewing_agent_id == "worker-0"
            assert app._transcript_panel.has_focus
            assert not app._agent_list.display
            assert not app._composer.show_cursor

            await pilot.press("escape")
            await pilot.pause()
            assert app.viewing_agent_id is None
            assert app._agent_list.display
            assert app._agent_list.has_focus
            assert app._main_focus_target == "agent_list"

            store.record(SubagentLifecycle(
                timestamp=3.0,
                source="subagent",
                agent_uuid="worker-0",
                agent_type="worker",
                phase="end",
            ))
            app._schedule_agent_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not app._agent_list.display
            assert app._composer.has_focus

    asyncio.run(scenario())


def test_visible_agent_list_animates_only_running_row_without_worker(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        store = AgentViewStore(clock=lambda: 100.0)
        store.register_foreground("main", "main")
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="subagent",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="start",
            task="分析代码结构",
        ))
        app = _app(store)
        async with app.run_test(size=(100, 30)) as pilot:
            app._tick_timer.pause()
            app._schedule_agent_refresh(now=0.01)
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app._agent_list.display
            assert app._agent_ids == ["main", "worker-0"]
            app._agent_list.index = 1
            children = tuple(app._agent_list.children)
            main_label = next(iter(children[0].query(Static)))
            worker_label = next(iter(children[1].query(Static)))
            main_updates: list[str] = []
            worker_updates: list[str] = []

            main_update = main_label.update
            worker_update = worker_label.update

            def record_main(content, *, layout=True) -> None:
                main_updates.append(str(content))
                main_update(content, layout=layout)

            def record_worker(content, *, layout=True) -> None:
                worker_updates.append(
                    content.plain if isinstance(content, Text) else str(content)
                )
                worker_update(content, layout=layout)

            monkeypatch.setattr(main_label, "update", record_main)
            monkeypatch.setattr(worker_label, "update", record_worker)
            presentation_workers: list[str] = []
            monkeypatch.setattr(
                app,
                "_run_presentation_worker",
                lambda _factory, *, group: presentation_workers.append(group),
            )

            app._schedule_agent_refresh(now=0.05)
            for index in range(1, 11):
                app._schedule_agent_refresh(now=index / 10 + 0.01)

            assert main_updates == []
            assert [update[0] for update in worker_updates] == list(
                "⠙⠹⠸⠼⠴⠦⠧⠇⠏⠋"
            )
            assert presentation_workers == []
            assert tuple(app._agent_list.children) == children
            assert app._agent_list.index == 1

            worker_updates.clear()
            app._agent_list.display = False
            app._schedule_agent_refresh(now=1.11)
            app._schedule_agent_refresh(now=1.21)
            assert worker_updates == []

            app._agent_list.display = True
            app._schedule_agent_refresh(now=1.21)
            assert [update[0] for update in worker_updates] == ["⠹"]

    asyncio.run(scenario())


def test_modal_controls_are_keyboard_only() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            loop = asyncio.get_running_loop()
            choice = ChoiceMenu(
                timestamp=1.0,
                source="test",
                options=[("one", "一"), ("two", "二")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice)
            await pilot.pause()
            options = app._interaction_slot.query_one(KeyboardOptionList)
            assert options.has_focus
            assert options.highlighted == 0

            app.screen.set_focus(None)
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert options.has_focus
            await pilot.click("#dialog-shell", offset=(1, 0))
            await pilot.pause()
            assert options.has_focus

            await pilot.click(options, offset=(2, 1))
            await pilot.pause()
            assert not choice.future.done()
            assert options.highlighted == 0
            assert options.has_focus

            await pilot.press("down", "enter")
            await pilot.pause()
            assert await choice.future == "two"

            choice_input = ChoiceInputMenu(
                timestamp=2.0,
                source="test",
                options=[("auto", "自动")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice_input)
            await pilot.pause()
            navigation = app._interaction_slot.query_one("#choice-input-options", KeyboardNavigation)
            dialog_input = app._interaction_slot.query_one("#dialog-input", TextArea)
            assert navigation.has_focus

            await pilot.click(dialog_input)
            await pilot.pause()
            assert navigation.has_focus
            assert dialog_input.text == ""

            await pilot.press("down", "a", "b")
            assert dialog_input.has_focus
            assert dialog_input.text == "ab"
            cursor = dialog_input.cursor_location
            await pilot.click(dialog_input, offset=(0, 0))
            await pilot.pause()
            assert dialog_input.has_focus
            assert dialog_input.cursor_location == cursor
            assert dialog_input.text == "ab"

            await pilot.click(navigation)
            await pilot.pause()
            assert dialog_input.has_focus
            await pilot.press("enter")
            assert await choice_input.future == '{"choice": "", "text": "ab"}'

            form = FormMenu(
                timestamp=3.0,
                source="test",
                questions=[FormQuestion(
                    question="模式",
                    options=[("safe", "安全")],
                )],
                future=loop.create_future(),
            )
            await app.coordinator.submit(form)
            await pilot.pause()
            form_widget = app._interaction_slot.query_one(InlineFormWidget)
            form_input = app._interaction_slot.query_one("#custom-input-0", TextArea)
            assert form_widget.has_focus

            await pilot.click(form_input)
            await pilot.pause()
            assert form_widget.has_focus
            await pilot.press("down", "x")
            assert form_input.has_focus
            assert form_input.text == "x"
            await pilot.click(app._interaction_slot.query_one("#label-0-0"))
            await pilot.pause()
            assert form_input.has_focus
            assert form_widget.rows[0] == 1

            form.future.cancel()
            await pilot.pause()

    asyncio.run(scenario())


def test_form_custom_row_is_inline_and_focus_shows_cursor() -> None:
    """自定义回答行常驻选项末尾：导航到该行即聚焦输入框，无底部独立输入框。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(
                question="模式",
                options=[("safe", "安全")],
            )])
            slot = app._interaction_slot
            assert len(slot.query("#dialog-input")) == 0
            custom_input = slot.query_one("#custom-input-0", TextArea)
            assert slot.query_one("#custom-row-0").display

            await pilot.press("down")
            await pilot.pause()
            assert app.screen.focused is custom_input
            assert custom_input.has_focus
            assert custom_input.show_cursor
            await pilot.press("a", "b")
            assert custom_input.text == "ab"

            await pilot.press("enter", "enter")
            assert await form.future == '{"answers": ["ab"], "discussion": ""}'

    asyncio.run(scenario())


def test_form_free_text_question_focuses_custom_input_on_mount() -> None:
    """无选项自由题挂载后直接聚焦其它输入行。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(question="说明")])
            custom_input = app._interaction_slot.query_one("#custom-input-0", TextArea)
            assert custom_input.has_focus
            await pilot.press("o", "k")
            assert custom_input.text == "ok"
            await pilot.press("enter", "enter")
            assert await form.future == '{"answers": ["ok"], "discussion": ""}'

    asyncio.run(scenario())


def test_form_custom_input_autogrows_to_four_lines_then_scrolls() -> None:
    """其它输入默认 1 行，随内容增高到 4 行后内部滚动，清空后回到 1 行。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_form(app, pilot, [FormQuestion(question="说明")])
            custom_input = app._interaction_slot.query_one("#custom-input-0", TextArea)
            assert int(custom_input.styles.height.value) == 1

            await pilot.press(
                "shift+enter", "shift+enter", "shift+enter",
                "shift+enter", "shift+enter", "x",
            )
            await pilot.pause()
            assert custom_input.wrapped_document.height >= 6
            assert int(custom_input.styles.height.value) == 4
            assert custom_input.scroll_y > 0

            custom_input.clear()
            await pilot.pause()
            assert int(custom_input.styles.height.value) == 1

    asyncio.run(scenario())


def test_form_discussion_row_swaps_with_custom_row_and_persists() -> None:
    """讨论行与其它行在同一位置互换；文本在切换间保持。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(
                question="模式",
                options=[("safe", "安全")],
            )])
            slot = app._interaction_slot
            custom_row = slot.query_one("#custom-row-0")
            discussion_row = slot.query_one("#discussion-row")
            discussion_input = slot.query_one("#discussion-input", TextArea)
            assert custom_row.display
            assert not discussion_row.display

            await pilot.press("tab")
            await pilot.pause()
            assert not custom_row.display
            assert discussion_row.display
            assert app.screen.focused is discussion_input
            await pilot.press("n", "o", "t", "e")
            assert discussion_input.text == "note"

            await pilot.press("tab")
            await pilot.pause()
            assert custom_row.display
            assert not discussion_row.display

            await pilot.press("tab")
            await pilot.pause()
            assert discussion_input.text == "note"
            await pilot.press("enter")
            assert await form.future == '{"answers": [""], "discussion": "note"}'

    asyncio.run(scenario())


def test_form_custom_texts_are_independent_per_question() -> None:
    """每题的其它输入独立保存，切换标签不丢失。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [
                FormQuestion(question="题一", options=[("a", "甲")], header="一"),
                FormQuestion(question="题二", options=[("b", "乙")], header="二"),
            ])
            slot = app._interaction_slot
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("o", "n", "e")
            assert slot.query_one("#custom-input-0", TextArea).text == "one"

            await pilot.press("right")
            await pilot.pause()
            second_input = slot.query_one("#custom-input-1", TextArea)
            assert second_input.text == ""
            await pilot.press("down")
            await pilot.pause()
            assert app.screen.focused is second_input
            await pilot.press("t", "w", "o")
            assert second_input.text == "two"

            await pilot.press("left")
            await pilot.pause()
            assert slot.query_one("#custom-input-0", TextArea).text == "one"

            await pilot.press("right", "right", "enter")
            payload = await form.future
            assert '"answers": ["one", "two"]' in payload

    asyncio.run(scenario())


def test_form_recommended_option_shows_suffix_component() -> None:
    """推荐项在 label 后紧跟独立 (推荐) 后缀组件。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(48, 24)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="模式",
                    options=[("a", "方案 **A**"), ("b", "方案 B")],
                    recommended=[True, False],
                )],
                markdown=True,
            )
            slot = app._interaction_slot
            label = slot.query_one("#label-0-0")
            suffix = slot.query_one("#recommended-0-0", Static)
            assert isinstance(label, Markdown)
            assert str(suffix.visual) == "(推荐)"
            children = list(suffix.parent.children)
            assert [child.id for child in children] == [
                "marker-0-0", "label-0-0", "recommended-0-0",
            ]
            assert label.region.right <= suffix.region.x
            assert suffix.region.right <= suffix.parent.region.right
            assert len(slot.query("#recommended-0-1")) == 0

    asyncio.run(scenario())


def test_form_question_label_and_description_follow_markdown_flag() -> None:
    """题干、选项标签与说明统一按 markdown 开关渲染。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(
                app, pilot,
                [FormQuestion(
                    question="**重要**决定",
                    options=[("a", "用 `code` 实现")],
                    descriptions=["细节**加粗**"],
                )],
                markdown=True,
            )
            slot = app._interaction_slot
            question_text = slot.query_one("#question-text-0")
            label = slot.query_one("#label-0-0")
            description = slot.query_one("#description-0-0")
            assert isinstance(question_text, Markdown)
            assert isinstance(label, Markdown)
            assert isinstance(description, Markdown)
            assert "form-description" in description.classes
            await pilot.pause()
            rendered = [
                [str(block.visual) for block in widget.query(Static)]
                for widget in (question_text, label, description)
            ]
            assert any("重要决定" in text for text in rendered[0])
            assert any("用 code 实现" in text for text in rendered[1])
            assert any("细节加粗" in text for text in rendered[2])
            assert all("**" not in text and "`" not in text for texts in rendered for text in texts)

            await pilot.press("escape")
            assert await form.future == ""

            await _open_form(app, pilot, [FormQuestion(
                question="**重要**决定",
                options=[("a", "用 `code` 实现")],
                descriptions=["细节**加粗**"],
            )])
            slot = app._interaction_slot
            plain_question = slot.query_one("#question-text-0")
            plain_label = slot.query_one("#label-0-0")
            plain_desc = slot.query_one("#description-0-0")
            assert isinstance(plain_question, SelectionStatic)
            assert isinstance(plain_label, SelectionStatic)
            assert isinstance(plain_desc, SelectionStatic)
            assert "**重要**" in str(plain_question.visual)
            assert "`code`" in str(plain_label.visual)
            assert "**加粗**" in str(plain_desc.visual)

    asyncio.run(scenario())


def test_form_multiline_label_keeps_logical_row_navigation() -> None:
    """多行选项标签不改变逻辑导航：Down 一次移动一个选项。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(
                app, pilot,
                [FormQuestion(
                    question="多行",
                    options=[("a", "第一行  \n第二行"), ("b", "短")],
                )],
                markdown=True,
            )
            slot = app._interaction_slot
            widget = slot.query_one(InlineFormWidget)
            assert slot.query_one("#label-0-0").region.height >= 2
            await pilot.press("down")
            await pilot.pause()
            assert widget.rows[0] == 1
            marker = slot.query_one("#marker-0-1", Static)
            assert str(marker.visual).startswith("❯")
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("enter")
            assert await form.future == '{"answers": ["b"], "discussion": ""}'

    asyncio.run(scenario())


def test_form_preview_updates_and_hides_descriptions() -> None:
    """有 preview 时不渲染说明组件；预览随当前选项更新。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(120, 36)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="预览",
                    options=[("a", "A"), ("b", "B")],
                    descriptions=["说明A", "说明B"],
                    previews=["预览A内容", "预览B内容"],
                )],
                markdown=True,
            )
            slot = app._interaction_slot
            slot.query_one("#options-0")
            assert len(slot.query(".form-description")) == 0
            pane = slot.query_one("#form-preview-pane")
            assert pane.display
            preview = slot.query_one("#form-preview", Markdown)
            await pilot.pause()
            blocks = [str(block.visual) for block in preview.query(Static)]
            assert any("预览A内容" in text for text in blocks)

            await pilot.press("down")
            await pilot.pause()
            await pilot.pause()
            blocks = [str(block.visual) for block in preview.query(Static)]
            assert any("预览B内容" in text for text in blocks)

    asyncio.run(scenario())


def test_form_preview_stacks_and_body_scrolls_in_compact_viewport() -> None:
    """紧凑视口下 preview 分栏上下堆叠不重叠；内容超高时正文内部滚动。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="紧凑布局",
                    options=[(f"v{index}", f"选项{index}") for index in range(12)],
                    descriptions=[f"说明{index}" for index in range(12)],
                    previews=["预览内容"] + [""] * 11,
                )],
                markdown=True,
            )
            await pilot.pause()
            slot = app._interaction_slot
            slot.query_one("#options-0")
            left = slot.query_one("#form-left")
            pane = slot.query_one("#form-preview-pane")
            assert pane.display
            assert left.region.bottom <= pane.region.y
            blocks = [
                str(block.visual)
                for block in slot.query_one("#form-preview", Markdown).query(Static)
            ]
            assert any("预览内容" in text for text in blocks)
            body = slot.query_one("#inline-form-body", VerticalScroll)
            assert body.max_scroll_y > 0

    asyncio.run(scenario())


def test_form_drag_select_copies_on_mac() -> None:
    """题干/选项文本支持鼠标拖选；macOS 选中即复制。"""
    async def scenario() -> None:
        app = _app(platform="darwin")
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="选择 **题干** 文本",
                    options=[("a", "选项 `标签` 文本")],
                )],
                markdown=True,
            )
            await pilot.pause()
            slot = app._interaction_slot
            start_block = slot.query_one("#question-text-0")
            end_block = slot.query_one("#label-0-0")
            await pilot.mouse_down(start_block, offset=(0, 0))
            end_region = end_block.content_region
            end = Offset(end_region.right - 1, end_region.y)
            app.mouse_position = end
            app.screen._forward_event(events.MouseMove(
                end_block,
                *end,
                end_region.width - 1,
                0,
                1,
                False,
                False,
                False,
                screen_x=end.x,
                screen_y=end.y,
            ))
            await pilot.mouse_up(offset=end)
            await pilot.pause()
            selected = app.screen.get_selected_text()
            assert "选择 题干 文本" in selected
            assert "选项 标签 文本" in selected
            assert "**" not in selected
            assert "`" not in selected
            assert app.clipboard == selected

    asyncio.run(scenario())


def test_form_preview_code_block_and_trailing_newline_drag_no_crash() -> None:
    """preview 代码块与尾随换行内容拖选不崩溃。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(120, 36)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="预览\n",
                    options=[("a", "标签\n")],
                    previews=["```python\nprint('hi')\n```"],
                )],
                markdown=True,
            )
            await pilot.pause()
            slot = app._interaction_slot
            slot.query_one("#options-0")
            preview = slot.query_one("#form-preview", Markdown)
            blocks = list(preview.query(Static))
            assert blocks
            target = blocks[-1]
            region = target.content_region
            await pilot.mouse_down(target, offset=(0, 0))
            end = Offset(region.x + 2, region.y)
            app.mouse_position = end
            app.screen._forward_event(events.MouseMove(
                target,
                *end,
                2,
                0,
                1,
                False,
                False,
                False,
                screen_x=end.x,
                screen_y=end.y,
            ))
            await pilot.mouse_up(offset=end)
            await pilot.pause()
            assert app.fatal_error is None
            assert app.is_running
            assert app.screen.get_selected_text()

    asyncio.run(scenario())


def test_form_inputs_stay_keyboard_only_under_mouse() -> None:
    """其它/讨论输入框不响应鼠标聚焦与拖选。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_form(app, pilot, [FormQuestion(
                question="模式",
                options=[("safe", "安全")],
            )])
            slot = app._interaction_slot
            custom_input = slot.query_one("#custom-input-0", TextArea)
            widget = slot.query_one(InlineFormWidget)
            assert widget.has_focus

            await pilot.mouse_down(custom_input, offset=(0, 0))
            await pilot.hover(custom_input, offset=(2, 0))
            await pilot.mouse_up(custom_input, offset=(2, 0))
            await pilot.pause()
            assert custom_input.selected_text == ""
            assert widget.has_focus

    asyncio.run(scenario())


def test_form_multi_select_toggles_and_combines_custom_text() -> None:
    """多选题：空格/数字勾选与反选，自定义文本按选项序追加在勾选值之后。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(
                question="组件",
                options=[("a", "甲"), ("b", "乙"), ("c", "丙")],
                multi_select=True,
            )])
            slot = app._interaction_slot
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("space")
            await pilot.press("3")
            await pilot.pause()
            assert "[x]" in str(slot.query_one("#marker-0-0", Static).visual)
            assert "[ ]" in str(slot.query_one("#marker-0-1", Static).visual)
            assert "[x]" in str(slot.query_one("#marker-0-2", Static).visual)

            await pilot.press("down", "x", "y")
            await pilot.press("right", "enter")
            assert await form.future == '{"answers": ["a、c、xy"], "discussion": ""}'

    asyncio.run(scenario())


def test_form_single_select_custom_text_and_option_are_exclusive() -> None:
    """单选互斥：输入清空已选项；选中选项清空已输入文本。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(
                question="模式",
                options=[("safe", "安全")],
            )])
            slot = app._interaction_slot
            widget = slot.query_one(InlineFormWidget)
            await pilot.press("1")
            await pilot.pause()
            assert widget.checked[0] == {0}

            await pilot.press("left")
            await pilot.press("down", "z")
            await pilot.pause()
            assert widget.checked[0] == set()
            assert widget.custom[0] == "z"

            await pilot.press("up")
            await pilot.press("space")
            await pilot.pause()
            assert widget.checked[0] == {0}
            assert widget.custom[0] == ""
            assert slot.query_one("#custom-input-0", TextArea).text == ""

            form.future.cancel()
            await pilot.pause()

    asyncio.run(scenario())


def test_form_restores_focus_after_app_focus() -> None:
    """终端重新激活后，焦点恢复到当前状态对应组件且文本保留。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await _open_form(app, pilot, [FormQuestion(
                question="模式",
                options=[("safe", "安全")],
            )])
            slot = app._interaction_slot
            widget = slot.query_one(InlineFormWidget)
            custom_input = slot.query_one("#custom-input-0", TextArea)
            discussion_input = slot.query_one("#discussion-input", TextArea)

            assert widget.has_focus
            app.screen.set_focus(None)
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert widget.has_focus

            await pilot.press("down", "x")
            assert custom_input.has_focus
            app.screen.set_focus(None)
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert custom_input.has_focus
            assert custom_input.text == "x"
            assert custom_input.show_cursor

            await pilot.press("tab", "n")
            assert discussion_input.has_focus
            app.screen.set_focus(None)
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert discussion_input.has_focus
            assert discussion_input.text == "n"

            form.future.cancel()
            await pilot.pause()

    asyncio.run(scenario())


def test_form_preview_pane_hides_on_empty_and_restores() -> None:
    """预览随当前选项切换：空 preview 隐藏面板，返回时恢复内容。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(120, 36)) as pilot:
            await _open_form(
                app, pilot,
                [FormQuestion(
                    question="预览",
                    options=[("a", "A"), ("b", "B")],
                    previews=["预览A内容", ""],
                )],
                markdown=True,
            )
            slot = app._interaction_slot
            pane = slot.query_one("#form-preview-pane")
            assert pane.display
            await pilot.press("down")
            await pilot.pause()
            assert not pane.display
            await pilot.press("up")
            await pilot.pause()
            await pilot.pause()
            assert pane.display
            blocks = [
                str(block.visual)
                for block in slot.query_one("#form-preview", Markdown).query(Static)
            ]
            assert any("预览A内容" in text for text in blocks)

    asyncio.run(scenario())


def test_modal_fifo_and_permission_over_transcript() -> None:
    async def scenario() -> None:
        store = AgentViewStore()
        store.register_foreground("main", "main")
        for index in range(2):
            store.record(SubagentLifecycle(
                timestamp=float(index),
                source="subagent",
                agent_uuid=f"worker-{index}",
                agent_type="worker",
                phase="start",
            ))
            store.record(ResponseDelta(
                timestamp=float(index),
                source="test",
                caller_uuid=f"worker-{index}",
                caller_agent_type="worker",
                content="\n\n".join(
                    f"agent {index} paragraph {line}" for line in range(50)
                ),
            ))
        app = _app(store)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.coordinator.open_live_transcript("worker-0")
            await pilot.pause()
            assert app.viewing_agent_id == "worker-0"
            assert not app._composer_shell.display
            assert not app._separator_top.display
            assert not app._separator_bottom.display
            assert not app._agent_list.display
            assert app._transcript_panel.has_focus
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app._transcript_panel.has_focus
            assert not app._composer.has_focus
            assert app._transcript_panel.max_scroll_y > 0
            app._transcript_panel.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            app._save_transcript_position()
            assert not app._transcript_positions["worker-0"].follow
            app.switch_transcript(1)
            await app.workers.wait_for_complete()
            await _wait_for_transcript_render(app, pilot)
            assert app.viewing_agent_id == "worker-1"
            app.switch_transcript(-1)
            await app.workers.wait_for_complete()
            await _wait_for_transcript_render(app, pilot)
            assert app.viewing_agent_id == "worker-0"
            assert app._transcript_panel.scroll_y == 0

            permission_future = asyncio.get_running_loop().create_future()
            choice_future = asyncio.get_running_loop().create_future()
            permission = PermissionMenu(
                timestamp=3.0,
                source="test",
                tool_name="shell",
                detail="echo test",
                caller_agent_type="worker",
                caller_uuid="worker-0",
                future=permission_future,
            )
            choice = ChoiceMenu(
                timestamp=4.0,
                source="test",
                prompt="继续？",
                options=[("ok", "继续"), ("stop", "停止")],
                caller_agent_type="worker",
                caller_uuid="worker-1",
                future=choice_future,
            )
            await app.coordinator.submit(permission)
            await app.coordinator.submit(choice)
            await pilot.pause()
            assert isinstance(app.coordinator.inline_widget, InlineSelectionWidget)
            assert app.coordinator.inline_widget.request is permission
            assert app.viewing_agent_id == "worker-0"
            assert app.coordinator.pending_summary == (1, "worker")
            modal_focus = app.focused
            assert modal_focus is not None
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert app.focused is modal_focus
            assert not app._composer.has_focus

            await pilot.press("1")
            await pilot.pause()
            assert await permission_future == "yes"
            assert app.viewing_agent_id == "worker-0"
            assert isinstance(app.coordinator.inline_widget, InlineSelectionWidget)
            assert app.coordinator.inline_widget.request is choice
            await pilot.press("1")
            await pilot.pause()
            assert await choice_future == "ok"
            await app.coordinator.wait_idle()
            assert app.viewing_agent_id == "worker-0"
            assert not app._composer_shell.display
            app.close_transcript()
            await pilot.pause()
            assert app._composer_shell.display
            assert app._separator_top.display
            assert app._separator_bottom.display
            assert app._agent_list.display

    asyncio.run(scenario())


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
            app.close_transcript()
            await pilot.pause()
            assert app._input_status.display

    asyncio.run(scenario())


def test_selection_stays_stable_across_scroll_and_platform_copy_rules() -> None:
    async def scenario() -> None:
        app = _app(platform="win32")
        # 高度需保证 #history 视口能容纳整数个段落块：块高与视口高不整除时，
        # 自动滚动会在一次 tick 内跨过块边界，选区长度出现回退而非单调增长。
        async with app.run_test(size=(100, 31)) as pilot:
            for index in range(36):
                await app.append_markdown(
                    f"### 选择段落 {index:02d}\n\n用于验证跨视口连续文本选择。"
                )
            await pilot.pause()
            app._history.scroll_to(
                y=app._history.max_scroll_y // 2,
                animate=False,
                immediate=True,
            )
            await pilot.pause()
            history_region = app._history.content_region
            await pilot.mouse_down(
                app._history,
                offset=(1, 1),
            )
            assert app.screen._select_state is not None
            assert app.screen._select_state.start.container is app._history
            edge = (history_region.x + 2, history_region.bottom - 1)
            initial_scroll_y = app._history.scroll_y
            app.mouse_position = Offset(*edge)
            app.screen._forward_event(events.MouseMove(
                app._history,
                *edge,
                0,
                8,
                1,
                False,
                False,
                False,
                screen_x=edge[0],
                screen_y=edge[1],
            ))
            lengths: list[int] = []
            start_scrolled_out = False
            for _ in range(12):
                await pilot.pause(0.1)
                if selected := app.screen.get_selected_text():
                    lengths.append(len(selected))
                delta = app._history.scroll_y - initial_scroll_y
                if delta >= 2:
                    start_scrolled_out = True
            assert start_scrolled_out
            assert len(lengths) >= 3
            assert lengths == sorted(lengths), lengths
            await pilot.mouse_up(offset=edge)
            assert app.screen._auto_select_scroll_timer is None
            released_scroll_y = app._history.scroll_y
            await pilot.pause(0.15)
            assert app._history.scroll_y == released_scroll_y

            app.clear_selection()
            text = "可复制的生产 TUI 文本"
            entry_id = app._history.append_entry(text)
            app._history.jump_to_tail()
            await app._history.wait_for_reflow()
            start, _end = app._history.entry_ranges[entry_id]
            app.screen.selections = {
                app._history: Selection(Offset(0, start), Offset(len(text), start))
            }
            selected = app.screen.get_selected_text()
            assert selected == text
            await pilot.press("ctrl+c")
            assert app.clipboard == selected

        mac_app = _app(platform="darwin")
        async with mac_app.run_test(size=(90, 28)) as pilot:
            target = "selected history"
            text = f"prefix {target} suffix"
            mac_app._history.append_entry(text, spacing=0)
            await pilot.pause()
            start_x = len("prefix ")
            end_x = start_x + len(target)
            content = mac_app._history.content_region
            widget_offset = (
                content.x - mac_app._history.region.x + start_x,
                content.y - mac_app._history.region.y,
            )
            await pilot.mouse_down(mac_app._history, offset=widget_offset)
            end = Offset(content.x + end_x - 1, content.y)
            mac_app.mouse_position = end
            mac_app.screen._forward_event(events.MouseMove(
                mac_app._history,
                *end,
                end_x - start_x,
                0,
                1,
                False,
                False,
                False,
                screen_x=end.x,
                screen_y=end.y,
            ))
            await pilot.mouse_up(offset=end)
            await pilot.pause()
            assert mac_app.screen.get_selected_text() == target
            assert mac_app.clipboard == target

    asyncio.run(scenario())


def test_history_entries_have_uniform_spacing() -> None:
    """条目间距由逻辑行区间统一给出，不依赖内容里的尾随换行。"""

    async def scenario() -> None:
        app = _app(platform="linux")
        async with app.run_test(size=(80, 40)) as pilot:
            # 混合 flush_round 的两种产出：带尾随换行的常规条目，
            # 以及中断态 / 无参数无结果的成功条目（本就不带尾随换行）。
            await app.append_output(Text("✔ read (0.01s)\n  src/main.py\n"))
            await app.append_output(Text("⋯ read  已中断"))
            await app.append_output(Text("✔ shell (0.02s)\n  ls\n"))
            await app.append_output(Text("✔ ping (0.01s)"))
            await pilot.pause()

            entries = list(app._history.entries)
            assert len(entries) == 4

            # 内容不再残留尾随换行；每个条目的 Rich 行区间包含一行留白。
            heights = [
                app._history.entry_ranges[entry.id][1]
                - app._history.entry_ranges[entry.id][0]
                for entry in entries
            ]
            assert heights == [3, 2, 3, 2]
            plains = [
                entry.content.plain if isinstance(entry.content, Text) else entry.content
                for entry in entries
            ]
            assert not any(plain.endswith("\n") for plain in plains)

            # 行区间连续，留白属于前一个条目。
            gaps = [
                app._history.entry_ranges[after.id][0]
                - app._history.entry_ranges[before.id][1]
                for before, after in zip(entries, entries[1:])
            ]
            assert gaps == [0, 0, 0]

    asyncio.run(scenario())


def test_history_entry_trailing_newline_row_is_selectable_without_crash() -> None:
    async def scenario() -> None:
        # win32：ctrl+c 走 _selected_text() 复制路径，覆盖真实取词入口。
        app = _app(platform="win32")
        async with app.run_test(size=(80, 24)) as pilot:
            entry_id = app._history.append_entry(
                Text("✔ read (0.01s)\n  src/main.py\n")
            )
            await pilot.pause()

            start, end = app._history.entry_ranges[entry_id]
            assert end - start == 4
            offset = Offset(0, start + 2)

            # 起点落在空行时不越界，也不返回 padding。
            app.screen.selections = {app._history: Selection(offset, None)}
            assert app.screen.get_selected_text() == ""

            app.screen.selections = {app._history: Selection(None, offset)}
            assert app.screen.get_selected_text() == "✔ read (0.01s)\n  src/main.py"

            app.screen.selections = {
                app._history: Selection(Offset(2, start), None)
            }
            assert app.screen.get_selected_text() == "read (0.01s)\n  src/main.py"

            app.screen.selections = {app._history: Selection(None, None)}
            assert app.screen.get_selected_text() == "✔ read (0.01s)\n  src/main.py"
            await pilot.press("ctrl+c")
            assert app.clipboard == "✔ read (0.01s)\n  src/main.py"
            assert app.fatal_error is None
            assert app.is_running

    asyncio.run(scenario())


def test_dialog_prompt_trailing_newline_row_is_selectable_without_crash() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)) as pilot:
            loop = asyncio.get_running_loop()
            # ask_user 等场景的 prompt 由模型自撰，可能以换行结尾。
            choice = ChoiceMenu(
                timestamp=1.0,
                source="test",
                prompt="选择一项：\n",
                options=[("a", "A"), ("b", "B")],
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice)
            await pilot.pause()
            prompt = app._interaction_slot.query_one(".dialog-prompt", SelectionStatic)
            assert prompt.region.height == 2

            app.screen.selections = {prompt: Selection(Offset(0, 1), None)}
            assert app.screen.get_selected_text() == ""
            assert app.fatal_error is None
            assert app.is_running

            await pilot.press("escape")
            assert await choice.future == ""

    asyncio.run(scenario())


def test_reverse_selection_tracks_tool_result_without_crossing_its_end() -> None:
    async def scenario() -> None:
        app = _app(platform="linux")
        async with app.run_test(size=(80, 24)) as pilot:
            assert isinstance(app.screen, SelectionScreen)
            for index in range(24):
                await app.append_output(
                    f"● main {index:02d} · 本轮 2 工具\n"
                    f"  ✔ read-{index:02d}  ⎿ first-{index:02d}\n"
                    f"  ✔ shell-{index:02d}  ⎿ second-{index:02d}"
                )
            await pilot.pause()
            target = app._history.entries[16]
            start, end = app._history.entry_ranges[target.id]
            app.screen.selections = {
                app._history: Selection(
                    Offset(50, end - 2),
                    Offset(0, start),
                )
            }
            selected = app.screen.get_selected_text()
            assert selected is not None
            assert "● main 16 · 本轮 2 工具" in selected
            assert selected.endswith("✔ shell-16  ⎿ second-16")
            assert "main 17" not in selected
            assert app.is_running

    asyncio.run(scenario())


def test_native_clipboard_is_serial_latest_wins_and_osc52_is_fallback() -> None:
    class SlowClipboard:
        supported = True

        def __init__(self, results: list[bool]) -> None:
            self.results = iter(results)
            self.release = asyncio.Event()
            self.started = asyncio.Event()
            self.calls: list[str] = []
            self.active = 0
            self.max_active = 0

        async def copy_async(self, text: str) -> bool:
            self.calls.append(text)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
            await self.release.wait()
            self.active -= 1
            return next(self.results)

    async def scenario() -> None:
        app = AgentTuiApp(
            AgentViewStore(),
            [],
            TurnClock(),
            lambda: None,
            lambda: False,
            lambda: None,
            platform_name="darwin",
        )
        clipboard = SlowClipboard([True, True])
        app._native_clipboard = clipboard
        async with app.run_test(size=(80, 24)) as pilot:
            driver_writes: list[str] = []
            app._driver.write = driver_writes.append
            app.copy_to_clipboard("first")
            await clipboard.started.wait()
            app.copy_to_clipboard("second")
            app.copy_to_clipboard("last")
            assert app.clipboard == "last"
            clipboard.release.set()
            worker = app._clipboard_worker_task
            assert worker is not None
            await worker
            assert clipboard.calls == ["first", "last"]
            assert clipboard.max_active == 1
            assert not [write for write in driver_writes if "\x1b]52;c;" in write]
            await pilot.pause()

        failed_app = AgentTuiApp(
            AgentViewStore(),
            [],
            TurnClock(),
            lambda: None,
            lambda: False,
            lambda: None,
            platform_name="win32",
        )
        failed_clipboard = SlowClipboard([False])
        failed_clipboard.release.set()
        failed_app._native_clipboard = failed_clipboard
        async with failed_app.run_test(size=(80, 24)):
            driver_writes: list[str] = []
            failed_app._driver.write = driver_writes.append
            failed_app.copy_to_clipboard("fallback")
            worker = failed_app._clipboard_worker_task
            assert worker is not None
            await worker
            osc52 = [write for write in driver_writes if "\x1b]52;c;" in write]
            assert len(osc52) == 1
            assert failed_app.clipboard == "fallback"

        linux_app = _app(platform="linux")
        async with linux_app.run_test(size=(80, 24)):
            driver_writes: list[str] = []
            linux_app._driver.write = driver_writes.append
            linux_app.copy_to_clipboard("linux fallback")
            osc52 = [write for write in driver_writes if "\x1b]52;c;" in write]
            assert len(osc52) == 1
            assert linux_app.clipboard == "linux fallback"

    asyncio.run(scenario())


def test_external_cancellation_cleans_modal_before_future_completion() -> None:
    async def scenario() -> None:
        clock = TurnClock()
        app = AgentTuiApp(
            AgentViewStore(),
            [],
            clock,
            lambda: None,
            lambda: False,
            lambda: None,
            native_clipboard=False,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            loop = asyncio.get_running_loop()
            first = PermissionMenu(
                timestamp=1.0,
                source="test",
                tool_name="shell",
                detail="pwd",
                future=loop.create_future(),
            )
            cancelled = ChoiceMenu(
                timestamp=2.0,
                source="test",
                options=[("ok", "继续")],
                future=loop.create_future(),
            )
            observed: list[tuple[object, object]] = []
            complete = first.complete

            def observe(value: str) -> None:
                observed.append((app.coordinator.active, app.coordinator.inline_widget))
                complete(value)

            first.complete = observe  # type: ignore[method-assign]
            await app.coordinator.submit(first)
            await app.coordinator.submit(cancelled)
            cancelled.future.cancel()
            await pilot.pause()
            assert app.coordinator.pending_summary == (0, None)
            await pilot.press("1")
            await pilot.pause()
            await asyncio.wait_for(app.coordinator.wait_idle(), timeout=1)
            assert first.future.result() == "yes"
            assert observed == [(None, None)]
            assert clock._human_wait_depth == 0

            external = PermissionMenu(
                timestamp=3.0,
                source="test",
                tool_name="write",
                detail="file",
                future=loop.create_future(),
            )
            await app.coordinator.submit(external)
            external.future.cancel()
            await pilot.pause()
            await asyncio.wait_for(app.coordinator.wait_idle(), timeout=1)
            assert app.coordinator.active is None
            assert app.coordinator.inline_widget is None
            assert clock._human_wait_depth == 0

    asyncio.run(scenario())


def test_permission_menu_settles_turn_elapsed_only_when_input_resumes(
    monkeypatch,
) -> None:
    """权限交互只暂停当前回合，回到主输入态时才统一结算一次。"""
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 30)) as pilot:
            app._turn_started = 1.0
            monkeypatch.setattr(app, "_turn_elapsed", lambda _now: 7.0)
            loop = asyncio.get_running_loop()
            permission = PermissionMenu(
                timestamp=1.0,
                source="test",
                tool_name="shell",
                detail="pwd",
                future=loop.create_future(),
            )
            next_input = InputMenu(
                timestamp=2.0,
                source="test",
                prompt="输入",
                future=loop.create_future(),
            )

            await app.coordinator.submit(permission)
            await app.coordinator.submit(next_input)
            await pilot.pause()
            assert app._session_elapsed == 0.0

            await pilot.press("1")
            assert await permission.future == "yes"
            await pilot.pause()

            assert app.coordinator.input_active
            assert app._session_elapsed == 7.0

    asyncio.run(scenario())


def test_close_cancels_active_model_menu_and_waits_for_removal() -> None:
    """关闭 UI 应能等待活动模型菜单卸载，不接受 coroutine 的限制不得泄漏。"""
    async def scenario() -> None:
        clock = TurnClock()
        app = AgentTuiApp(
            AgentViewStore(),
            [],
            clock,
            lambda: None,
            lambda: False,
            lambda: None,
            native_clipboard=False,
        )
        async with app.run_test(size=(80, 18)) as pilot:
            request = ModelMenu(
                timestamp=1.0,
                source="models",
                models=[("model-a", "provider/model-a")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                future=asyncio.get_running_loop().create_future(),
            )
            await app.coordinator.submit(request)
            await pilot.pause()
            widget = app.coordinator.inline_widget

            assert widget is not None
            assert widget.is_mounted
            assert clock._human_wait_depth == 1

            await asyncio.wait_for(app.coordinator.close(), timeout=1)
            await pilot.pause()

            assert request.future.cancelled()
            assert not widget.is_attached
            assert not app._interaction_slot.display
            assert not app._interaction_slot.children
            assert app.coordinator.active is None
            assert app.coordinator.inline_widget is None
            assert not app.coordinator._cleanup_tasks
            assert app.coordinator.is_idle
            assert clock._human_wait_depth == 0

    asyncio.run(scenario())


def test_stream_follow_and_dialog_text_inputs() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 32)) as pilot:
            event = ResponseDelta(
                timestamp=1.0,
                source="test",
                caller_uuid="main",
                caller_agent_type="main",
                content="",
            )
            await app.on_response_delta(
                event,
                "\n\n".join(f"stream paragraph {i}" for i in range(80)) + "\n\n",
            )
            await pilot.pause()
            await app._history.wait_for_refresh()
            assert app._history.scroll_y == app._history.max_scroll_y
            await app.end_response()
            assert app._history.entries[-1].markdown
            assert app._history.entries[-1].content == (
                "\n\n".join(f"stream paragraph {i}" for i in range(80)) + "\n\n"
            )
            app._history.scroll_to(
                y=max(0, app._history.max_scroll_y - 10),
                animate=False,
                immediate=True,
            )
            old_scroll_y = app._history.scroll_y
            await app.on_response_delta(
                event,
                "\n\n".join(f"next paragraph {i}" for i in range(20)) + "\n\n",
            )
            await pilot.pause()
            await app._history.wait_for_refresh()
            assert app._history.scroll_y == old_scroll_y
            await app.end_response()
            assert app._history.entries[-1].content == (
                "\n\n".join(f"next paragraph {i}" for i in range(20)) + "\n\n"
            )

            loop = asyncio.get_running_loop()
            choice_input = ChoiceInputMenu(
                timestamp=2.0,
                source="test",
                prompt="审核",
                options=[("auto", "自动"), ("manual", "手动")],
                default_index=2,
                future=loop.create_future(),
            )
            await app.coordinator.submit(choice_input)
            await pilot.pause()
            dialog_input = app._interaction_slot.query_one("#dialog-input")
            assert dialog_input.has_focus
            app.post_message(events.AppFocus())
            await pilot.pause()
            assert dialog_input.has_focus
            assert not app._composer.has_focus
            await pilot.press("1", "space", "2", "shift+enter", "x", "enter")
            result = await choice_input.future
            assert result == '{"choice": "", "text": "1 2\\nx"}'

            form = FormMenu(
                timestamp=3.0,
                source="test",
                questions=[
                    FormQuestion(question="说明"),
                    FormQuestion(
                        question="模式",
                        options=[("safe", "安全"), ("fast", "快速")],
                    ),
                ],
                future=loop.create_future(),
            )
            await app.coordinator.submit(form)
            await pilot.pause()
            await pilot.press("1", "space", "2", "shift+enter", "x")
            await pilot.press("right", "2", "right", "enter")
            payload = await form.future
            assert '"answers": ["1 2\\nx", "fast"]' in payload

    asyncio.run(scenario())


def test_session_history_is_bulk_hydrated_into_rich_lines() -> None:
    async def scenario() -> None:
        state = SessionState()
        state.append_user("恢复的问题", {"role": "user", "content": "恢复的问题"})
        state.record_event(ThinkingDelta(
            timestamp=2.0,
            source="model",
            content="恢复的思考",
            call_id="call-restore",
        ))
        state.record_event(ResponseDelta(
            timestamp=3.0,
            source="model",
            content="**恢复的回答**",
            call_id="call-restore",
        ))
        state.bind_model_message(
            {
                "role": "assistant",
                "content": "**恢复的回答**",
                "reasoning_content": "恢复的思考",
            },
            correlation_id="call-restore",
            kind="assistant",
        )
        app = _app()
        async with app.run_test(size=(90, 28)):
            await app.replace_session_history(state.visible_records())

            assert not app._history.children
            assert len(app._history.entries) == 5
            assert app._history.entries[0].content == "› 恢复的问题"
            assert app._history.entries[2].content == "恢复的思考"
            assert app._history.entries[2].markdown
            assert app._history.entries[4].content == "**恢复的回答**"
            assert app._history.entries[4].markdown
            assert "恢复的回答" in app.history_journal.snapshot()

    asyncio.run(scenario())


def test_up_key_recalls_history_after_submit_without_refresh() -> None:
    """回归：历史快照不在 refresh 之外缓存——提交后按上键应立刻回溯到新条目。

    根因：若 history_prev 读的是 refresh_input_history 时拉取的旧快照，
    运行中新提交的输入不会进入回溯，按上键无反应。

    语义：光标在第一行行首按上才进历史；首行非行首按上先跳到行首。
    """

    submitted: list[str] = []

    def provider() -> list[str]:
        return list(submitted)

    app = AgentTuiApp(
        AgentViewStore(),
        [],
        TurnClock(),
        lambda: None,
        lambda: False,
        lambda: None,
        get_input_history=provider,
        native_clipboard=False,
    )

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            app.refresh_input_history()
            composer = app.query_one("#input", Composer)
            composer.read_only = False
            composer.focus()
            await pilot.pause()

            # 空输入框（行首）按上：无历史，无反应
            await pilot.press("up")
            assert composer.text == ""

            # 之后才有新提交（provider 数据变化，但不再 refresh）
            submitted.append("第一条")
            submitted.append("第二条")
            await pilot.pause()

            # 光标在行首按上：立刻回溯到最新条目
            await pilot.press("up")
            assert composer.text == "第二条"
            # 载入历史后光标停在行首，连续按上逐条上溯
            await pilot.press("up")
            assert composer.text == "第一条"
            # 已在最早一条，再按上键无变化
            await pilot.press("up")
            assert composer.text == "第一条"
            # 下键前进，越过最新条还原草稿（空）
            await pilot.press("down")
            assert composer.text == "第二条"
            await pilot.press("down")
            assert composer.text == ""

    asyncio.run(scenario())


def test_up_key_on_first_line_moves_to_line_start_before_history() -> None:
    """光标在首行非行首按上先跳到行首；行首再按上才进历史；多行非首行按上正常上移。"""

    def provider() -> list[str]:
        return ["历史一", "历史二"]

    app = AgentTuiApp(
        AgentViewStore(),
        [],
        TurnClock(),
        lambda: None,
        lambda: False,
        lambda: None,
        get_input_history=provider,
        native_clipboard=False,
    )

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            app.refresh_input_history()
            composer = app.query_one("#input", Composer)
            composer.read_only = False
            composer.focus()
            await pilot.pause()

            # 单行输入，光标在行尾（首行非行首）按上 → 跳到行首，不进历史
            composer.load_text("hello")
            composer.move_cursor((0, 5))
            await pilot.press("up")
            assert composer.text == "hello"
            assert composer.cursor_location == (0, 0)
            # 行首再按上 → 进历史
            await pilot.press("up")
            assert composer.text == "历史二"

            # 还原草稿后测多行：光标在第 2 行按上 → 上移到第 1 行（不进历史）
            await pilot.press("down")  # 越过最新条还原草稿 "hello"
            assert composer.text == "hello"
            composer.load_text("第一行\n第二行")
            composer.move_cursor((1, 3))
            await pilot.press("up")
            assert composer.text == "第一行\n第二行"
            assert composer.cursor_location[0] == 0  # 上移到首行
            # 首行非行首按上 → 跳到行首
            await pilot.press("up")
            assert composer.cursor_location == (0, 0)
            assert composer.text == "第一行\n第二行"
            # 行首再按上 → 进历史
            await pilot.press("up")
            assert composer.text == "历史二"

    asyncio.run(scenario())


def test_up_key_on_soft_wrapped_line_moves_one_visual_line_before_history() -> None:
    """软折行草稿按上键应逐视觉行上移，到文首后才回溯历史。"""

    def provider() -> list[str]:
        return ["较早历史", "最新历史"]

    draft = "a" * 500
    app = AgentTuiApp(
        AgentViewStore(),
        [],
        TurnClock(),
        lambda: None,
        lambda: False,
        lambda: None,
        get_input_history=provider,
        native_clipboard=False,
    )

    async def scenario() -> None:
        async with app.run_test(size=(48, 24)) as pilot:
            app.refresh_input_history()
            composer = app.query_one("#input", Composer)
            composer.read_only = False
            composer.focus()
            composer.load_text(draft)
            await pilot.pause()

            assert composer.has_focus
            assert composer.text == draft
            assert "\n" not in composer.text
            wrapped_document = composer.wrapped_document
            assert wrapped_document.height >= 3

            composer.move_cursor(wrapped_document.offset_to_location(Offset(1, 2)))
            start_offset = wrapped_document.location_to_offset(composer.cursor_location)
            assert composer.cursor_location[0] == 0
            assert start_offset.y == 2

            for _ in range(start_offset.y):
                before = composer.cursor_location
                before_y = wrapped_document.location_to_offset(before).y
                expected = composer.get_cursor_up_location()
                expected_y = wrapped_document.location_to_offset(expected).y
                assert expected_y == before_y - 1

                await pilot.press("up")

                assert composer.text == draft
                assert composer.cursor_location == expected
                after_y = wrapped_document.location_to_offset(composer.cursor_location).y
                assert after_y == before_y - 1

            assert wrapped_document.location_to_offset(composer.cursor_location).y == 0
            assert composer.cursor_location != (0, 0)
            expected = composer.get_cursor_up_location()
            assert expected == (0, 0)

            await pilot.press("up")

            assert composer.text == draft
            assert composer.cursor_location == expected

            await pilot.press("up")
            assert composer.text == "最新历史"

    asyncio.run(scenario())


def test_composer_mouse_drag_selects_for_copy() -> None:
    """回归：Composer 放开鼠标拖选后，可拖选文本并经 _selected_text 取到（复制数据源）。

    根因：KeyboardTextArea._on_mouse_down 拦截 + ALLOW_SELECT=False 使输入框无法选中。
    修复后 Composer 恢复原生拖选；对话框的 KeyboardTextArea 仍保持仅键盘。
    """

    app = _app()

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#input", Composer)
            composer.read_only = False
            composer.focus()
            composer.load_text("hello world")
            await pilot.pause()

            # 类属性已放开拖选；对话框基类仍保持仅键盘
            assert Composer.ALLOW_SELECT is True
            from src.interfaces.tui.widgets import KeyboardTextArea
            assert KeyboardTextArea.ALLOW_SELECT is False

            # 经 pilot 真实路由做拖选（mouse_down → hover 移动 → mouse_up）
            await pilot.mouse_down(composer, offset=(0, 0))
            await pilot.hover(composer, offset=(5, 0))
            await pilot.mouse_up(composer, offset=(5, 0))
            await pilot.pause()

            assert composer.selected_text != ""
            # 复制数据源 _selected_text 应能取到聚焦 Composer 的选区
            assert app._selected_text() == composer.selected_text

    asyncio.run(scenario())


def test_model_menu_uses_vertical_models_and_horizontal_effort() -> None:
    """模型菜单应分别用上下键和左右键调整两个维度。"""
    app = _app()
    selection_color = Color.parse("#76d7c4")

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            loop = asyncio.get_running_loop()
            request = ModelMenu(
                timestamp=1.0,
                source="models",
                models=[("model-a", "model-a (one)"), ("model-b", "model-b (two)")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                model_index=0,
                effort_index=2,
                future=loop.create_future(),
            )
            await app.coordinator.submit(request)
            await pilot.pause()

            body = app._interaction_slot.query_one("#model-menu-body", KeyboardNavigation)
            effort = app._interaction_slot.query_one("#model-menu-effort", Static)
            assert body.has_focus
            body_text = body.render()
            effort_text = effort.render()
            assert "› model-a (one)" in body_text.plain
            assert "› high" in effort_text.plain
            assert [
                body_text.plain[span.start:span.end]
                for span in body_text.spans
                if span.style.foreground == selection_color
            ] == ["› model-a (one)"]
            assert [
                effort_text.plain[span.start:span.end]
                for span in effort_text.spans
                if span.style.foreground == selection_color
            ] == ["› high"]

            await pilot.press("down", "right")
            body_text = body.render()
            effort_text = effort.render()
            assert [
                body_text.plain[span.start:span.end]
                for span in body_text.spans
                if span.style.foreground == selection_color
            ] == ["› model-b (two)"]
            assert [
                effort_text.plain[span.start:span.end]
                for span in effort_text.spans
                if span.style.foreground == selection_color
            ] == ["› xhigh"]

            await pilot.press("enter")
            assert await request.future == (
                '{"model": "model-b", "reasoning_effort": "xhigh"}'
            )

            await pilot.pause()
            cancelled = ModelMenu(
                timestamp=2.0,
                source="models",
                models=[("model-a", "model-a")],
                efforts=["low", "medium", "high", "xhigh", "max"],
                future=loop.create_future(),
            )
            await app.coordinator.submit(cancelled)
            await pilot.pause()
            await pilot.press("escape")
            assert await cancelled.future == ""

    asyncio.run(scenario())


def test_model_menu_scrolls_models_while_effort_and_hint_stay_visible() -> None:
    """短窗口中模型独立滚动，强度和操作提示保持可见。"""
    app = _app()

    async def scenario() -> None:
        async with app.run_test(size=(80, 18)) as pilot:
            loop = asyncio.get_running_loop()
            request = ModelMenu(
                timestamp=1.0,
                source="models",
                models=[(f"model-{index}", f"model-{index}") for index in range(16)],
                efforts=["low", "medium", "high", "xhigh", "max"],
                model_index=0,
                effort_index=2,
                future=loop.create_future(),
            )
            await app.coordinator.submit(request)
            await pilot.pause()

            scroll = app._interaction_slot.query_one("#model-menu-scroll", VerticalScroll)
            body = app._interaction_slot.query_one("#model-menu-body", KeyboardNavigation)
            effort = app._interaction_slot.query_one("#model-menu-effort", Static)
            hint = app._interaction_slot.query_one(".dialog-hint", Static)
            interaction_region = app._interaction_slot.content_region

            assert scroll.max_scroll_y > 0
            assert effort.region.y >= interaction_region.y
            assert effort.region.bottom <= interaction_region.bottom
            assert hint.region.y >= interaction_region.y
            assert hint.region.bottom <= interaction_region.bottom
            effort_region = effort.region
            hint_region = hint.region

            await pilot.press(*(["down"] * 14))
            await pilot.pause()

            assert "› model-14" in str(body.render())
            assert scroll.scroll_y > 0
            selected_y = body.region.y + 15
            assert scroll.content_region.y <= selected_y < scroll.content_region.bottom
            assert effort.region == effort_region
            assert hint.region == hint_region
            assert "› high" in str(effort.render())
            assert "↑↓ 模型" in str(hint.render())

            await pilot.press("right", "enter")
            assert await request.future == (
                '{"model": "model-14", "reasoning_effort": "xhigh"}'
            )

    asyncio.run(scenario())
