"""Textual 生产 TUI 的关键交互回归测试。"""

from __future__ import annotations

import asyncio
import gc
import warnings

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
from src.events.types import PermissionNotice, ResponseDelta, SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.app import AgentTuiApp
from src.interfaces.tui.dialogs import InlineSelectionWidget, SelectionDialog
from src.interfaces.tui.widgets import (
    Composer,
    KeyboardNavigation,
    KeyboardOptionList,
    SelectionScreen,
    SelectionStatic,
)


def _app(
    store: AgentViewStore | None = None,
    *,
    platform: str = "darwin",
    get_model_info=None,
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
    )


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


def test_failed_history_mount_is_not_committed(monkeypatch) -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)):
            def fail_mount(_widget) -> None:
                raise RuntimeError("mount failed")

            monkeypatch.setattr(app._history, "mount", fail_mount)
            with pytest.raises(RuntimeError, match="mount failed"):
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
            await pilot.press("escape")
            assert await choice.future == ""
            await pilot.pause()
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
            widget = list(app._history.children)[-1]
            rendered = widget.render().plain
            assert "硬规则" in rendered
            assert detail in rendered
            # 60 列终端下长路径应折行（高度 > 1）而不是被截断。
            await pilot.pause()
            assert widget.size.height > 1

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
            options = app._history.query_one(KeyboardOptionList)
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
            navigation = app._history.query_one("#choice-input-options", KeyboardNavigation)
            dialog_input = app._history.query_one("#dialog-input", TextArea)
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
            form_body = app._history.query_one("#form-body", KeyboardNavigation)
            form_input = app._history.query_one("#dialog-input", TextArea)
            assert form_body.has_focus

            await pilot.click(form_input)
            await pilot.pause()
            assert form_body.has_focus
            await pilot.press("down", "x")
            assert form_input.has_focus
            assert form_input.text == "x"
            await pilot.click(form_body)
            await pilot.pause()
            assert form_input.has_focus

            form.future.cancel()
            await pilot.pause()

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
            visible = [
                block
                for block in app._history.query("MarkdownBlock")
                if block.region.overlaps(history_region)
            ]
            assert visible
            start_block = min(visible, key=lambda block: block.region.y)
            start_region = start_block.region.intersection(history_region)
            await pilot.mouse_down(
                start_block,
                offset=(
                    min(1, max(0, start_region.width - 1)),
                    start_region.y - start_block.region.y,
                ),
            )
            assert app.screen._select_state is not None
            assert app.screen._select_state.start.container is app._history
            edge = (history_region.x + 2, history_region.bottom - 1)
            initial_scroll_y = app._history.scroll_y
            initial_bottom = start_region.bottom
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
                if initial_bottom - delta <= history_region.y:
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
            target = Static("可复制的生产 TUI 文本", markup=False)
            await app._history.mount(target)
            await pilot.pause()
            app.screen.selections = {target: Selection(None, None)}
            selected = app.screen.get_selected_text()
            assert selected == "可复制的生产 TUI 文本"
            await pilot.press("ctrl+c")
            assert app.clipboard == selected

        mac_app = _app(platform="darwin")
        async with mac_app.run_test(size=(90, 28)) as pilot:
            target = Static("macOS 选中即复制", markup=False)
            await mac_app._history.mount(target)
            await pilot.pause()
            mac_app.screen.selections = {target: Selection(None, None)}
            mac_app.screen.post_message(events.TextSelected())
            await pilot.pause()
            assert mac_app.clipboard == "macOS 选中即复制"

    asyncio.run(scenario())


def test_history_entries_have_uniform_spacing() -> None:
    """条目间距由 CSS margin 统一给出，不再依赖内容里的尾随换行。"""

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

            entries = list(app._history.children)
            assert len(entries) == 4

            # 内容不再残留尾随换行；间距改由 padding-bottom 提供，
            # 因此每个条目高度 = 真实行数 + 1 行留白，中断态条目不再例外。
            heights = [entry.region.height for entry in entries]
            assert heights == [3, 2, 3, 2]
            plains = [str(entry.visual) for entry in entries]
            assert not any(plain.endswith("\n") for plain in plains)

            # 留白在条目内部，条目之间不再另有间隙。
            gaps = [
                after.region.y - (before.region.y + before.region.height)
                for before, after in zip(entries, entries[1:])
            ]
            assert gaps == [0, 0, 0]

    asyncio.run(scenario())


def test_history_entry_trailing_newline_row_is_selectable_without_crash() -> None:
    async def scenario() -> None:
        # win32：ctrl+c 走 _selected_text() 复制路径，覆盖真实取词入口。
        app = _app(platform="win32")
        async with app.run_test(size=(80, 24)) as pilot:
            # append_output 会剥掉尾随换行，这里直接挂载以复现 Textual 的越界场景：
            # 任何绕过 append_output 的挂载点（如 dialogs）仍可能拿到这种内容。
            target = SelectionStatic(
                Text("✔ read (0.01s)\n  src/main.py\n"),
                classes="history-static",
                markup=False,
            )
            await app._history.mount(target)
            await pilot.pause()

            # 末尾 "\n" 让 Static 比 splitlines() 多渲染一行空行
            # （再加 padding-bottom 的 1 行留白）。
            assert target.region.height == 4
            widget, offset = app.screen.get_widget_and_offset_at(
                target.region.x,
                target.region.y + 2,
            )
            assert widget is target
            assert offset == Offset(0, 2)

            # 起点落在空行：原生 Static 在此 IndexError 打崩整个 app。
            app.screen.selections = {target: Selection(offset, None)}
            assert app.screen.get_selected_text() == ""

            # 终点落在空行：收敛到末行行尾，而不是丢掉末行。
            app.screen.selections = {target: Selection(None, offset)}
            assert app.screen.get_selected_text() == "✔ read (0.01s)\n  src/main.py"

            # 未越界的选区行为与原生一致。
            app.screen.selections = {target: Selection(Offset(2, 0), None)}
            assert app.screen.get_selected_text() == "read (0.01s)\n  src/main.py"

            app.screen.selections = {target: Selection(None, None)}
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
            prompt = app._history.query_one(".dialog-prompt", SelectionStatic)
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
            summaries: list[Static] = []
            for index in range(24):
                await app.append_output(
                    f"● main {index:02d} · 本轮 2 工具\n"
                    f"  ✔ read-{index:02d}  ⎿ first-{index:02d}\n"
                    f"  ✔ shell-{index:02d}  ⎿ second-{index:02d}"
                )
                summaries.append(list(app._history.children)[-1])
            await pilot.pause()

            async def drag_up(target_index: int, title_y: int, *, reach_top: bool) -> None:
                target = summaries[target_index]
                app._history.scroll_to(
                    y=target.virtual_region.y - title_y,
                    animate=False,
                    immediate=True,
                )
                await pilot.pause()
                assert target.region.y == title_y
                await pilot.mouse_down(target, offset=(50, 2))
                history_region = app._history.content_region
                edge = (history_region.x, history_region.y)
                app.mouse_position = Offset(*edge)
                app.screen._forward_event(events.MouseMove(
                    app._history,
                    *edge,
                    0,
                    -8,
                    1,
                    False,
                    False,
                    False,
                    screen_x=edge[0],
                    screen_y=edge[1],
                ))
                timer = app.screen._auto_select_scroll_timer
                assert timer is not None
                assert timer._interval == 0.05

                lengths: list[int] = []
                for _ in range(12 if reach_top else 2):
                    await pilot.pause(0.1)
                    selected = app.screen.get_selected_text()
                    if selected:
                        lengths.append(len(selected))
                assert lengths == sorted(lengths), lengths
                selected = app.screen.get_selected_text()
                assert selected is not None
                heading = f"● main {target_index:02d} · 本轮 2 工具"
                second_result = f"✔ shell-{target_index:02d}  ⎿ second-{target_index:02d}"
                assert heading in selected
                assert selected.endswith(second_result)
                assert f"main {target_index + 1:02d}" not in selected

                if reach_top:
                    assert app._history.scroll_y == 0
                    assert app.screen._auto_select_scroll_timer is None
                await pilot.mouse_up(offset=edge)
                assert app.screen._auto_select_scroll_timer is None
                released_scroll_y = app._history.scroll_y
                await pilot.pause(0.15)
                assert app._history.scroll_y == released_scroll_y
                app.clear_selection()

            history_middle = app._history.content_region.y + 8
            await drag_up(18, history_middle, reach_top=False)
            await drag_up(12, app._history.content_region.y, reach_top=True)

            for _ in range(5):
                target = summaries[16]
                app._history.scroll_to(
                    y=target.virtual_region.y - app._history.content_region.y - 4,
                    animate=False,
                    immediate=True,
                )
                await pilot.pause()
                await pilot.mouse_down(target, offset=(40, 2))
                edge = (
                    app._history.content_region.x,
                    app._history.content_region.y,
                )
                app.mouse_position = Offset(*edge)
                app.screen._forward_event(events.MouseMove(
                    app._history,
                    *edge,
                    0,
                    -4,
                    1,
                    False,
                    False,
                    False,
                    screen_x=edge[0],
                    screen_y=edge[1],
                ))
                await pilot.pause(0.06)
                await pilot.mouse_up(offset=edge)
                assert app.screen._auto_select_scroll_timer is None
            await pilot.press("ctrl+l")
            assert app.screen._select_state is None
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
            dialog_input = app._history.query_one("#dialog-input")
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

            body = app._history.query_one("#model-menu-body", KeyboardNavigation)
            effort = app._history.query_one("#model-menu-effort", Static)
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

            scroll = app._history.query_one("#model-menu-scroll", VerticalScroll)
            body = app._history.query_one("#model-menu-body", KeyboardNavigation)
            effort = app._history.query_one("#model-menu-effort", Static)
            hint = app._history.query_one(".dialog-hint", Static)
            history_region = app._history.content_region

            assert scroll.max_scroll_y > 0
            assert effort.region.y >= history_region.y
            assert effort.region.bottom <= history_region.bottom
            assert hint.region.y >= history_region.y
            assert hint.region.bottom <= history_region.bottom
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
