"""TUI 测试共享辅助：App 工厂与等待、断言工具。"""

from __future__ import annotations

import asyncio

from src.events.menu import FormMenu
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.turn_clock import TurnClock
from src.interfaces.tui.app import AgentTuiApp
from src.interfaces.tui.history_log import HistoryLog
from src.interfaces.tui.render_policy import TuiRenderPolicy


def _track_render_concurrency(app) -> list[int]:
    """测试辅助：记录转录渲染的最大并发数，返回 [active, peak]。"""
    state = [0, 0]
    original = app.transcript.render

    async def tracked(request):
        state[0] += 1
        state[1] = max(state[1], state[0])
        try:
            return await original(request)
        finally:
            state[0] -= 1

    app.transcript.render = tracked
    return state


async def _follow_newest(history: HistoryLog) -> None:
    """测试辅助：沿滚到底部的用户路径翻到最新分页并跟随尾部。"""
    while history.has_newer:
        history._shift_page("newer")
        await history.wait_for_reflow()
    history.scroll_end(animate=False, immediate=True)
    history.anchor()
    await history.wait_for_reflow()


def _app(
    store: AgentViewStore | None = None,
    *,
    platform: str = "darwin",
    get_model_info=None,
    render_policy: TuiRenderPolicy | None = None,
    slash_commands: list[tuple[str, str]] | None = None,
    get_input_history=None,
) -> AgentTuiApp:
    return AgentTuiApp(
        store or AgentViewStore(),
        [("clear", "清理会话"), ("agents", "查看 Agent")]
        if slash_commands is None
        else slash_commands,
        TurnClock(),
        lambda: None,
        lambda: False,
        lambda: None,
        get_input_history=get_input_history,
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
            app.transcript.pending is None
            and app.transcript.active_renders == 0
            and app.transcript.rendered_id == app.transcript.viewing_agent_id
        ):
            await pilot.pause()
            return
        await pilot.pause(0.01)
    raise AssertionError("transcript render did not become idle")
