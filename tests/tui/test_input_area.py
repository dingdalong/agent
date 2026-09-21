"""输入区控制器（InputAreaController）：补全菜单、输入区高度与历史回溯回归。"""

from __future__ import annotations

import asyncio

import pytest
from textual.geometry import Offset

from src.events.menu import ChoiceMenu, InputMenu
from src.events.types import SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore
from tests.tui.support import (
    _app,
    _wait_for_transcript_render,
)

_COMMANDS = [
    ("clear", "清理会话"),
    ("claude", "切换 Claude"),
    ("agents", "查看 Agent"),
]


async def _open_input(app, pilot):
    """提交 InputMenu 进入普通输入态，返回该请求的 future。"""
    future = asyncio.get_running_loop().create_future()
    await app.coordinator.submit(
        InputMenu(timestamp=1.0, source="test", prompt="输入", future=future)
    )
    await pilot.pause()
    return future


def _completion_text(app) -> str:
    return str(app.input_area.completion.visual)


@pytest.mark.integration
def test_input_area_height_follows_visual_lines_and_caps() -> None:
    """输入区高度按折行后的视觉行数取值，并在 1-8 行之间封顶。"""

    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(48, 24)) as pilot:
            await _open_input(app, pilot)
            composer = app.input_area.composer
            shell = app.input_area.shell
            assert shell.styles.height.value == 1

            app.input_area.load_text("a\nb\nc")
            await pilot.pause()
            assert shell.styles.height.value == 3

            app.input_area.load_text("\n".join(str(index) for index in range(12)))
            await pilot.pause()
            assert shell.styles.height.value == 8

            app.input_area.clear()
            await pilot.pause()
            assert shell.styles.height.value == 1

            # 软折行（无显式 \n）同样按视觉行数增高，未封顶时逐行对应
            for length in (60, 90, 120, 150):
                app.input_area.load_text("w" * length)
                await pilot.pause()
                wrapped = composer.wrapped_document.height
                if 1 < wrapped < 8:
                    assert shell.styles.height.value == wrapped
                    break
            else:
                raise AssertionError("未找到落在 1-8 视觉行之间的软折行样本")

            app.input_area.load_text("w" * 500)
            await pilot.pause()
            assert composer.wrapped_document.height > 8
            assert shell.styles.height.value == 8

    asyncio.run(scenario())


@pytest.mark.integration
def test_completion_menu_filters_by_slash_prefix() -> None:
    """只有未加空格的斜杠前缀才给候选，候选只保留前缀匹配项。"""

    async def scenario() -> None:
        app = _app(slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_input(app, pilot)
            assert not app.input_area.completion_visible

            app.input_area.load_text("/cl")
            await pilot.pause()
            assert app.input_area.completion_visible
            assert app.input_area.completion.display
            rendered = _completion_text(app)
            assert "❯ /clear  —  清理会话" in rendered
            assert "/claude" in rendered
            assert "/agents" not in rendered

            # 大小写不敏感，且前缀无匹配时整体隐藏
            app.input_area.load_text("/CL")
            await pilot.pause()
            assert "/claude" in _completion_text(app)

            app.input_area.load_text("/zzz")
            await pilot.pause()
            assert not app.input_area.completion_visible
            assert not app.input_area.completion.display

    asyncio.run(scenario())


@pytest.mark.integration
def test_completion_menu_caps_at_eight_rows() -> None:
    """候选多于 8 条时只渲染前 8 行，避免补全菜单挤占历史区。"""

    async def scenario() -> None:
        commands = [(f"cmd{index}", f"命令 {index}") for index in range(12)]
        app = _app(slash_commands=commands)
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_input(app, pilot)
            app.input_area.load_text("/")
            await pilot.pause()

            rendered = _completion_text(app)
            assert len(rendered.splitlines()) == 8
            assert "/cmd0" in rendered
            assert "/cmd7" in rendered
            assert "/cmd8" not in rendered

    asyncio.run(scenario())


@pytest.mark.integration
def test_tab_cycles_completion_then_enter_applies_before_submitting() -> None:
    """Tab 循环切换候选；Enter 只应用候选，需再次 Enter 才提交输入。"""

    async def scenario() -> None:
        app = _app(slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            future = await _open_input(app, pilot)
            app.input_area.load_text("/cl")
            await pilot.pause()
            assert "❯ /clear" in _completion_text(app)

            await pilot.press("tab")
            assert "❯ /claude" in _completion_text(app)
            await pilot.press("tab")
            assert "❯ /clear" in _completion_text(app)
            # 跳过首项后回环选中第二项
            await pilot.press("tab")
            assert "❯ /claude" in _completion_text(app)

            await pilot.press("enter")
            assert app.input_area.composer.text == "/claude "
            assert not future.done()
            assert not app.input_area.completion_visible

            await pilot.press("enter")
            await pilot.pause()
            assert (await future).strip() == "/claude"

    asyncio.run(scenario())


@pytest.mark.integration
def test_escape_hides_completion_without_touching_text() -> None:
    """Esc 只收起补全菜单，不清空已输入的斜杠前缀。"""

    async def scenario() -> None:
        app = _app(slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_input(app, pilot)
            app.input_area.load_text("/cl")
            await pilot.pause()
            assert app.input_area.completion_visible

            await pilot.press("escape")
            assert not app.input_area.completion_visible
            assert not app.input_area.completion.display
            assert app.input_area.composer.text == "/cl"

    asyncio.run(scenario())


@pytest.mark.integration
def test_completion_hidden_outside_bare_slash_prefix() -> None:
    """含空格或非斜杠开头的文本不给候选。"""

    async def scenario() -> None:
        app = _app(slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_input(app, pilot)

            app.input_area.load_text("/cl now")
            await pilot.pause()
            assert not app.input_area.completion_visible

            app.input_area.load_text("hello /cl")
            await pilot.pause()
            assert not app.input_area.completion_visible

            app.input_area.load_text("/cl")
            await pilot.pause()
            assert app.input_area.completion_visible

    asyncio.run(scenario())


@pytest.mark.integration
def test_completion_hidden_while_viewing_transcript() -> None:
    """查看 Agent 转录期间即使仍在输入态也不给候选。"""

    async def scenario() -> None:
        store = AgentViewStore()
        store.record(SubagentLifecycle(
            timestamp=1.0,
            source="test",
            agent_uuid="worker-0",
            agent_type="worker",
            phase="end",
            messages=[{"role": "assistant", "content": "worker 输出"}],
        ))
        store.flush_completed()
        app = _app(store, slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            await _open_input(app, pilot)
            app.input_area.load_text("/cl")
            await pilot.pause()
            assert app.input_area.completion_visible

            assert await app.transcript.open("worker-0", ["worker-0"], invoked=False)
            await _wait_for_transcript_render(app, pilot)
            assert app.coordinator.input_active
            assert app.transcript.viewing_agent_id == "worker-0"

            app.input_area.load_text("/cle")
            await pilot.pause()
            assert not app.input_area.completion_visible

    asyncio.run(scenario())


@pytest.mark.integration
def test_completion_hidden_in_processing_and_inline_widget_states() -> None:
    """处理态与内嵌交互激活时不给候选。"""

    async def scenario() -> None:
        app = _app(slash_commands=_COMMANDS)
        async with app.run_test(size=(100, 32)) as pilot:
            future = await _open_input(app, pilot)
            app.input_area.load_text("hi")
            await pilot.press("enter")
            await pilot.pause()
            assert await future == "hi"
            assert not app.coordinator.input_active

            app.input_area.load_text("/cl")
            await pilot.pause()
            assert not app.input_area.completion_visible

            choice = ChoiceMenu(
                timestamp=2.0,
                source="test",
                options=[("one", "一")],
                future=asyncio.get_running_loop().create_future(),
            )
            await app.coordinator.submit(choice)
            await pilot.pause()
            assert app.coordinator.inline_widget_active

            app.input_area.load_text("/cli")
            await pilot.pause()
            assert not app.input_area.completion_visible

    asyncio.run(scenario())


@pytest.mark.integration
def test_up_key_recalls_history_after_submit_without_refresh() -> None:
    """回归：历史快照不在 refresh 之外缓存——提交后按上键应立刻回溯到新条目。

    根因：若 history_previous 读的是 reset_history 时拉取的旧快照，
    运行中新提交的输入不会进入回溯，按上键无反应。

    语义：光标在第一行行首按上才进历史；首行非行首按上先跳到行首。
    """

    submitted: list[str] = []

    def provider() -> list[str]:
        return list(submitted)

    app = _app(get_input_history=provider)

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await _open_input(app, pilot)
            app.input_area.reset_history()
            composer = app.input_area.composer
            await pilot.pause()

            # 空输入框（行首）按上：无历史，无反应
            await pilot.press("up")
            assert composer.text == ""

            # 之后才有新提交（provider 数据变化，但不再 reset_history）
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


@pytest.mark.integration
def test_reset_history_discards_cursor_and_draft() -> None:
    """reset_history 复位回溯游标：之后按上键从最新条目重新开始，草稿也重新采集。"""

    def provider() -> list[str]:
        return ["历史一", "历史二"]

    app = _app(get_input_history=provider)

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await _open_input(app, pilot)
            app.input_area.reset_history()
            composer = app.input_area.composer

            app.input_area.load_text("旧草稿")
            await pilot.press("up")
            assert composer.text == "历史二"

            app.input_area.reset_history()
            app.input_area.load_text("新草稿")
            await pilot.press("up")
            # 复位后从最新条重新开始，而不是继续上溯到最早条
            assert composer.text == "历史二"
            await pilot.press("down")
            # 越过最新条还原的是复位后采集的草稿
            assert composer.text == "新草稿"

    asyncio.run(scenario())


@pytest.mark.integration
def test_session_switch_resets_recall_cursor() -> None:
    """会话切换（/clear、/resume 走同一入口）后旧游标不得延续到新会话历史。"""

    entries = ["甲一", "甲二", "甲三", "甲四", "甲五"]
    app = _app(get_input_history=lambda: list(entries))

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await _open_input(app, pilot)
            composer = app.input_area.composer

            for _ in range(4):
                await pilot.press("up")
            assert composer.text == "甲二"

            # 切到条数更少的会话：旧游标属于上一会话，不能继续使用
            entries[:] = ["乙一", "乙二", "乙三"]
            app.reload_session_state()
            await pilot.pause()
            assert composer.text == ""

            await pilot.press("up")
            assert composer.text == "乙三"
            await pilot.press("down")
            assert composer.text == ""

    asyncio.run(scenario())


@pytest.mark.integration
def test_up_key_on_first_line_moves_to_line_start_before_history() -> None:
    """光标在首行非行首按上先跳到行首；行首再按上才进历史；多行非首行按上正常上移。"""

    def provider() -> list[str]:
        return ["历史一", "历史二"]

    app = _app(get_input_history=provider)

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await _open_input(app, pilot)
            app.input_area.reset_history()
            composer = app.input_area.composer
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


@pytest.mark.integration
def test_up_key_on_soft_wrapped_line_moves_one_visual_line_before_history() -> None:
    """软折行草稿按上键应逐视觉行上移，到文首后才回溯历史。"""

    def provider() -> list[str]:
        return ["较早历史", "最新历史"]

    draft = "a" * 500
    app = _app(get_input_history=provider)

    async def scenario() -> None:
        async with app.run_test(size=(48, 24)) as pilot:
            await _open_input(app, pilot)
            app.input_area.reset_history()
            composer = app.input_area.composer
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
