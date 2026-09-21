"""转录控制器（切换、渲染调度、滚动位置）回归测试。"""

from __future__ import annotations

import asyncio
import gc
import warnings

import pytest
from textual import events
from textual.geometry import Offset
from textual.widgets import Markdown

from src.events.types import ResponseDelta, SubagentLifecycle
from src.interfaces.agent_view_store import AgentViewStore

from tests.tui.support import (
    _app,
    _track_render_concurrency,
    _wait_for_transcript_render,
)


@pytest.mark.integration
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
            await app.transcript.open("worker-0", ["worker-0", "worker-1"], invoked=False)
            await pilot.pause()
            app.transcript.switch(1)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert app.transcript.viewing_agent_id == "worker-1"
            assert "unexpected message" in app.transcript.content.source

    asyncio.run(scenario())


@pytest.mark.integration
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
        renders = _track_render_concurrency(app)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.transcript.open(
                "worker-0",
                ["worker-0", "worker-1"],
                invoked=False,
            )
            for _ in range(50):
                app.transcript.switch(1)
                app.transcript.switch(-1)
            await _wait_for_transcript_render(app, pilot)
            assert app.transcript.viewing_agent_id == "worker-0"
            assert app.transcript.rendered_id == "worker-0"
            assert renders[1] == 1
            assert app.transcript.merged_requests > 0
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


@pytest.mark.integration
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
            await app.transcript.open(
                "worker-0",
                ["worker-0", "worker-1"],
                invoked=False,
            )
            await _wait_for_transcript_render(app, pilot)
            app.transcript.panel.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            app.transcript.save_position()
            assert app.transcript.positions["worker-0"].scroll_y == 0

            app.transcript.switch(1)
            await _wait_for_transcript_render(app, pilot)
            assert app.transcript.panel.scroll_y > 0

            wait_for_refresh = app.transcript.panel.wait_for_refresh

            async def wait_for_refresh_after_tick() -> None:
                app._tick()
                await wait_for_refresh()

            monkeypatch.setattr(
                app.transcript.panel,
                "wait_for_refresh",
                wait_for_refresh_after_tick,
            )
            app.transcript.switch(-1)
            await _wait_for_transcript_render(app, pilot)

            assert app.transcript.positions["worker-0"].scroll_y == 0
            assert app.transcript.panel.scroll_y == 0

    asyncio.run(scenario())


@pytest.mark.integration
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
        renders = _track_render_concurrency(app)
        event = ResponseDelta(
            timestamp=2.0,
            source="test",
            caller_uuid="main",
            caller_agent_type="main",
            content="",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await app.transcript.open(
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
                    app.transcript.switch(1)
                    await asyncio.sleep(0)
                    app.transcript.switch(-1)
                    await asyncio.sleep(0)

            await asyncio.wait_for(
                asyncio.gather(stream_response(), switch_agents()),
                timeout=8,
            )
            await _wait_for_transcript_render(app, pilot)
            assert app.transcript.viewing_agent_id == "worker-0"
            assert app.transcript.rendered_id == "worker-0"
            assert renders[1] == 1
            assert app.transcript.worker_task is not None
            assert not app.transcript.worker_task.done()
            assert app.is_running

    asyncio.run(scenario())


@pytest.mark.integration
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


@pytest.mark.integration
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
            assert app.transcript.viewing_agent_id == "worker-0"
            assert app.transcript.panel.has_focus

    asyncio.run(scenario())


@pytest.mark.integration
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
            await app.transcript.open("worker-0", ["missing"], invoked=False)
            app.transcript.ids = ["missing"]
            app.transcript.switch(1)
            await pilot.pause()
            assert app.transcript.viewing_agent_id is None
            assert app.is_running

    asyncio.run(scenario())
