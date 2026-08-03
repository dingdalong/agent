"""WindowManager scheduling and lifecycle tests for the Inline TUI."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text

from src.events.bus import EventBus
from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    MenuRequest,
    PermissionMenu,
    TranscriptView,
    UiRequest,
)
from src.events.types import Event
from src.app.app import AgentApp
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.base import UserInterface
from src.interfaces.inline.controller import InlineController
from src.interfaces.inline.window_manager import DialogOutcome, WindowEntry, WindowManager
from src.interfaces.output_router import OutputRouter


class _QueueingUi(UserInterface):
    """Minimal UI that accepts requests without awaiting their readers."""

    def __init__(self) -> None:
        """Initialize the accepted-request collection."""
        super().__init__()
        self.accepted: list[UiRequest] = []

    async def _accept_ui_request(self, request: UiRequest) -> bool:
        """Record a request and report that the asynchronous frontend owns it."""
        self.accepted.append(request)
        return True

    async def _write(self, message: str, markdown: bool = False) -> None:
        """Ignore output in the test frontend."""

    async def _read_input(self, prompt: str, default: str = "", markdown: bool = False) -> str:
        """Return an unused input answer."""
        return "unused"

    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
        suggested_rules: list[str] | None = None,
        mcp_server_rule: str | None = None,
    ) -> str:
        """Return an unused permission answer."""
        return "unused"

    async def _read_choice(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """Return an unused selection answer."""
        return "unused"

    async def _read_form(
        self,
        prompt: str,
        questions: list[FormQuestion],
        markdown: bool = False,
    ) -> str:
        """Return an unused form answer."""
        return "unused"

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """Return an unused choice-input answer."""
        return "unused"

    async def _read_transcript_view(self, uuid: str) -> str:
        """Return an unused view answer."""
        return ""


def _build_reset_app(
    ui: UserInterface,
    event_bus: EventBus,
    memory_mgr: object | None,
    session_context: list[str] | None = None,
) -> tuple[AgentApp, SimpleNamespace, AgentViewStore]:
    """Build the minimal real-bus AgentApp used by reset integration tests.

    Args:
        ui: Frontend receiving events from the application consumer.
        event_bus: Real bus owned by the application.
        memory_mgr: Optional reload probe or emitter.
        session_context: Initial shared session context.

    Returns:
        Application, mutable dependency namespace, and shared view store.
    """
    store = AgentViewStore()
    deps = SimpleNamespace(
        ui=ui,
        session_id="old-session",
        session_context=list(session_context or []),
        memory_mgr=memory_mgr,
        tools_mgr=None,
        permission_mgr=None,
        config_mgr=None,
        plugin_mgr=None,
        hooks_mgr=None,
        plan_mgr=None,
        role_mgr=None,
        event_bus=event_bus,
        plan_mode_controller=None,
    )
    app = AgentApp(
        deps=deps,
        agent_view_store=store,
        output_router=OutputRouter(ui, store),
    )
    return app, deps, store


class _InterruptUi:
    """Record application interrupt calls without opening a terminal."""

    def __init__(self, order: list[str]) -> None:
        """Initialize the shared lifecycle-order log."""
        self._order = order

    def cancel_active_input(self) -> bool:
        """Record synchronous interaction cancellation."""
        self._order.append("cancel")
        return True

    async def wait_interactions_idle(self) -> None:
        """Record asynchronous runner cleanup."""
        self._order.append("idle")


class _InterruptBus:
    """Record output and join ordering for an interrupted application turn."""

    def __init__(self, order: list[str]) -> None:
        """Initialize the shared lifecycle-order log."""
        self._order = order

    async def request_output(self, content: str) -> None:
        """Record the interrupt output request.

        Args:
            content: Output requested by AgentApp.

        Returns:
            None.
        """
        assert content
        self._order.append("output")

    async def join(self) -> None:
        """Record completion of EventBus consumer work."""
        self._order.append("join")


def _input(source: str = "ui", caller: str | None = None) -> InputMenu:
    """Build an input request with a future owned by the current loop."""
    request = InputMenu(timestamp=0.0, source=source, prompt="input", caller_agent_type=caller)
    request.future = asyncio.get_running_loop().create_future()
    return request


def _permission(source: str = "permission", caller: str | None = None) -> PermissionMenu:
    """Build a permission request with a future owned by the current loop."""
    request = PermissionMenu(
        timestamp=0.0,
        source=source,
        tool_name="shell",
        detail="pwd",
        caller_agent_type=caller,
    )
    request.future = asyncio.get_running_loop().create_future()
    return request


def _transcript(uuid: str) -> TranscriptView:
    """Build a transcript view request with a future owned by the current loop."""
    request = TranscriptView(timestamp=0.0, source="ui", uuid=uuid)
    request.future = asyncio.get_running_loop().create_future()
    return request


def _choice() -> ChoiceMenu:
    """Build a two-option selection request with a loop-owned future."""
    request = ChoiceMenu(
        timestamp=0.0,
        source="ui",
        prompt="choose",
        options=[("one", "One"), ("two", "Two")],
    )
    request.future = asyncio.get_running_loop().create_future()
    return request


def _form() -> FormMenu:
    """Build a one-question form request with a loop-owned future."""
    request = FormMenu(
        timestamp=0.0,
        source="ui",
        prompt="form",
        questions=[FormQuestion(question="Answer")],
    )
    request.future = asyncio.get_running_loop().create_future()
    return request


def _choice_input() -> ChoiceInputMenu:
    """Build an options-plus-input request with a loop-owned future."""
    request = ChoiceInputMenu(
        timestamp=0.0,
        source="ui",
        prompt="choice input",
        options=[("one", "One"), ("two", "Two")],
    )
    request.future = asyncio.get_running_loop().create_future()
    return request


def test_dialogs_are_fifo_and_expose_the_first_waiting_source() -> None:
    """A dialog never preempts another dialog, and queue status names its source.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Complete two queued dialogs and inspect observable FIFO state.

        Returns:
            None.
        """
        gates: dict[int, asyncio.Future[str]] = {}
        started: list[object] = []

        async def run_dialog(request: object) -> str:
            started.append(request)
            return await gates[id(request)]

        manager = WindowManager(run_dialog)
        first = _input(source="main", caller="main")
        second = _permission(source="permission", caller="reviewer")
        gates[id(first)] = asyncio.get_running_loop().create_future()
        gates[id(second)] = asyncio.get_running_loop().create_future()

        manager.submit(first)
        await asyncio.sleep(0)
        manager.submit(second)

        assert manager.active_window is not None
        assert manager.active_window.request is first
        assert manager.pending_summary == (1, "reviewer")
        assert started == [first]

        gates[id(first)].set_result("hello")
        for _ in range(20):
            if started == [first, second]:
                break
            await asyncio.sleep(0)

        assert first.future is not None and first.future.result() == "hello"
        assert manager.active_window is not None
        assert manager.active_window.request is second
        assert manager.pending_summary == (0, None)
        assert started == [first, second]

        gates[id(second)].set_result("yes")
        await manager.wait_idle()

        assert second.future is not None and second.future.result() == "yes"
        assert manager.window_stack == ()

    asyncio.run(scenario())


def test_dialog_cleanup_precedes_public_future_completion() -> None:
    """The next caller never observes a completed request as an active window."""

    async def scenario() -> None:
        gate = asyncio.get_running_loop().create_future()

        async def run_dialog(_request: MenuRequest) -> str:
            return await gate

        manager = WindowManager(run_dialog)
        request = _input()
        observed_active_windows: list[object] = []
        complete = request.complete

        def observe_completion(value: str) -> None:
            """Record the active window at the public completion boundary."""
            observed_active_windows.append(manager.active_window)
            complete(value)

        request.complete = observe_completion  # type: ignore[method-assign]
        manager.submit(request)
        await asyncio.sleep(0)

        gate.set_result("done")
        await manager.wait_idle()

        assert observed_active_windows == [None]
        assert request.future is not None and request.future.result() == "done"

    asyncio.run(scenario())


def test_core_status_reports_the_first_queued_dialog_source() -> None:
    """The status line exposes the FIFO queue count and first caller identity."""

    async def scenario() -> None:
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        gate = asyncio.get_running_loop().create_future()

        async def read_menu(_request: MenuRequest) -> str:
            """Keep the active answer window open while the queue is rendered."""
            return await gate

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        active = _input(source="main", caller="main")
        queued = _permission(source="permission", caller="reviewer")
        try:
            await controller.on_event(active)
            await controller.on_event(queued)
            await asyncio.sleep(0)

            rendered = fragment_list_to_text(to_formatted_text(controller._render_core_status()))

            assert "等待 1：reviewer" in rendered
        finally:
            await controller._window_manager.close()

    asyncio.run(scenario())


def test_ui_request_hook_releases_the_event_consumer_without_settling_future() -> None:
    """A TTY-style hook can own a request while its EventBus caller keeps waiting."""

    async def scenario() -> None:
        ui = _QueueingUi()
        request = _permission(caller="child")

        await ui.on_event(request)

        assert ui.accepted == [request]
        assert request.future is not None and not request.future.done()

    asyncio.run(scenario())


def test_reset_gate_rejects_ui_request_before_stream_finalization() -> None:
    """A request entering during reset is rejected before any stream await.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Suspend stream cleanup and verify the reset gate still rejects immediately.

        Returns:
            None.
        """
        ui = _QueueingUi()
        stream_cleanup_started = asyncio.Event()
        release_stream_cleanup = asyncio.Event()

        async def block_stream_cleanup(_event: Event) -> None:
            """Record and suspend an invalid pre-rejection stream cleanup call.

            Args:
                _event: Event passed to stream finalization.

            Returns:
                None.
            """
            stream_cleanup_started.set()
            await release_stream_cleanup.wait()

        ui._end_streams_for = block_stream_cleanup  # type: ignore[method-assign]
        request = _permission(caller="child")
        event_task: asyncio.Task[None] | None = None
        try:
            async with ui.reset_session_interactions():
                event_task = asyncio.create_task(ui.on_event(request))
                await asyncio.sleep(0)

                assert request.future is not None and request.future.cancelled()
                assert event_task.done()
                assert not stream_cleanup_started.is_set()

            release_stream_cleanup.set()
            await asyncio.wait_for(event_task, timeout=1)
            assert ui.accepted == []
        finally:
            release_stream_cleanup.set()
            if event_task is not None and not event_task.done():
                event_task.cancel()
            if event_task is not None:
                await asyncio.gather(event_task, return_exceptions=True)

    asyncio.run(scenario())


def test_event_bus_consumes_permission_while_transcript_future_is_pending() -> None:
    """A pending /agents view does not block a later control event in the consumer."""

    async def scenario() -> None:
        bus = EventBus()
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        permission_gate = asyncio.get_running_loop().create_future()

        async def read_menu(request: MenuRequest) -> str:
            """Hold only the permission answer window until the test releases it."""
            assert isinstance(request, PermissionMenu)
            return await permission_gate

        async def consume() -> None:
            """Route bus events through the TTY controller under test."""
            async for event in bus.subscribe():
                await controller.on_event(event)

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        consumer = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0)
            view_task = asyncio.create_task(bus.request_transcript_view("child-bus"))
            await asyncio.wait_for(bus.join(), timeout=1)

            assert controller._transcript_visible
            assert not view_task.done()

            permission_task = asyncio.create_task(
                bus.request_permission("shell", "pwd", caller_agent_type="child"),
            )
            for _ in range(20):
                if controller._top_window_kind == "permission":
                    break
                await asyncio.sleep(0)

            assert controller._top_window_kind == "permission"
            assert not view_task.done()

            permission_gate.set_result("yes")
            assert await asyncio.wait_for(permission_task, timeout=1) == "yes"
            await controller.wait_interactions_idle()

            assert controller._transcript_visible
            assert controller._window_manager.close_transcript()
            assert await asyncio.wait_for(view_task, timeout=1) == ""
        finally:
            bus.close()
            await controller._window_manager.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_event_bus_join_waits_for_call_soon_delivery() -> None:
    """EventBus quiescence includes a delivery scheduled in the current ready wave.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Schedule emit beside join and block its consumer until join is observed.

        Returns:
            None.
        """
        bus = EventBus()
        consumer_started = asyncio.Event()
        release_consumer = asyncio.Event()
        join_returned = False

        async def consume() -> None:
            """Block while processing the single scheduled event.

            Returns:
                None.
            """
            async for _event in bus.subscribe():
                consumer_started.set()
                await release_consumer.wait()
                return

        async def release_after_barrier_check() -> None:
            """Release the consumer after observing whether join returned early.

            Returns:
                None.
            """
            await consumer_started.wait()
            await asyncio.sleep(0)
            try:
                assert not join_returned
            finally:
                release_consumer.set()

        consumer = asyncio.create_task(consume())
        release_task = asyncio.create_task(release_after_barrier_check())
        try:
            await asyncio.sleep(0)
            asyncio.get_running_loop().call_soon(
                asyncio.create_task,
                bus.request_output("late delivery"),
            )
            await bus.join()
            join_returned = True
            await asyncio.wait_for(release_task, timeout=1)
        finally:
            release_consumer.set()
            if not release_task.done():
                release_task.cancel()
            await asyncio.gather(release_task, return_exceptions=True)
            bus.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_event_bus_ui_request_rejection_gate_is_reentrant() -> None:
    """An inner rejection gate cannot reopen requests owned by an outer gate.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Emit requests inside nested gates and after both gates close.

        Returns:
            None.
        """
        bus = EventBus()

        outer_request = _permission()
        with bus.reject_ui_requests():
            await bus.emit(outer_request)
            assert outer_request.future is not None and outer_request.future.cancelled()

            inner_request = _permission()
            with bus.reject_ui_requests():
                await bus.emit(inner_request)
            assert inner_request.future is not None and inner_request.future.cancelled()

            resumed_outer_request = _permission()
            await bus.emit(resumed_outer_request)
            assert (
                resumed_outer_request.future is not None
                and resumed_outer_request.future.cancelled()
            )

        accepted_request = _permission()
        await bus.emit(accepted_request)
        assert accepted_request.future is not None and not accepted_request.future.done()
        accepted_request.cancel()

    asyncio.run(scenario())


def test_session_reset_drains_queued_bus_request_before_state_mutation() -> None:
    """Reset drains an old queued UiRequest through the real app consumer first.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Queue a request behind blocked output, then reset the application session.

        Returns:
            None.
        """
        bus = EventBus()
        output_started = asyncio.Event()
        release_output = asyncio.Event()
        reloads: list[str] = []

        class BlockingUi(_QueueingUi):
            """Hold the consumer on one output event before an old UI request."""

            async def _write(self, message: str, markdown: bool = False) -> None:
                """Block the deliberately leading event in the consumer.

                Args:
                    message: Output content.
                    markdown: Whether the content is Markdown.

                Returns:
                    None.
                """
                assert message == "blocked"
                assert not markdown
                output_started.set()
                await release_output.wait()

        class ReloadProbe:
            """Record the first shared-state mutation during reset."""

            def reload(self) -> None:
                """Record Manager mutation.

                Returns:
                    None.
                """
                reloads.append("manager")

        ui = BlockingUi()
        app, deps, _store = _build_reset_app(
            ui,
            bus,
            ReloadProbe(),
            ["old context"],
        )
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")
        consumer = asyncio.create_task(app._consume_events())
        old_request_task: asyncio.Task[str] | None = None
        reset_task: asyncio.Task[object] | None = None
        try:
            await asyncio.sleep(0)
            await bus.request_output("blocked")
            await asyncio.wait_for(output_started.wait(), timeout=1)
            old_request_task = asyncio.create_task(
                bus.request_permission("shell", "pwd", caller_agent_type="child"),
            )
            await asyncio.sleep(0)
            assert not old_request_task.done()

            with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
                reset_task = asyncio.create_task(app._reset_session(source="clear"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                assert reloads == []
                assert deps.session_id == "old-session"

                release_output.set()
                result = await asyncio.wait_for(reset_task, timeout=1)

            assert result is new_agent
            assert reloads == ["manager"]
            assert old_request_task.cancelled()
            assert ui.accepted == []
        finally:
            release_output.set()
            if reset_task is not None and not reset_task.done():
                reset_task.cancel()
            if old_request_task is not None and not old_request_task.done():
                old_request_task.cancel()
            await asyncio.gather(
                *(task for task in (reset_task, old_request_task) if task is not None),
                return_exceptions=True,
            )
            bus.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_session_reset_drains_request_already_finalizing_streams() -> None:
    """Reset waits for a pre-gate UiRequest already inside stream finalization.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Suspend request stream cleanup before reset and release it under the gate.

        Returns:
            None.
        """
        bus = EventBus()
        ui = _QueueingUi()
        stream_cleanup_started = asyncio.Event()
        release_stream_cleanup = asyncio.Event()
        reloads: list[str] = []

        async def block_stream_cleanup(event: Event) -> None:
            """Suspend the in-flight request before its post-await gate check.

            Args:
                event: Event currently being finalized by the UI.

            Returns:
                None.
            """
            assert isinstance(event, UiRequest)
            stream_cleanup_started.set()
            await release_stream_cleanup.wait()

        class ReloadProbe:
            """Record shared-state mutation after consumer quiescence."""

            def reload(self) -> None:
                """Record Manager reload.

                Returns:
                    None.
                """
                reloads.append("manager")

        ui._end_streams_for = block_stream_cleanup  # type: ignore[method-assign]
        app, deps, _store = _build_reset_app(ui, bus, ReloadProbe())
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")
        consumer = asyncio.create_task(app._consume_events())
        request_task: asyncio.Task[str] | None = None
        reset_task: asyncio.Task[object] | None = None
        try:
            await asyncio.sleep(0)
            request_task = asyncio.create_task(
                bus.request_permission("shell", "pwd", caller_agent_type="child"),
            )
            await asyncio.wait_for(stream_cleanup_started.wait(), timeout=1)

            with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
                reset_task = asyncio.create_task(app._reset_session(source="clear"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                assert reloads == []
                assert deps.session_id == "old-session"

                release_stream_cleanup.set()
                result = await asyncio.wait_for(reset_task, timeout=1)

            assert result is new_agent
            assert request_task.cancelled()
            assert ui.accepted == []
            assert reloads == ["manager"]
        finally:
            release_stream_cleanup.set()
            if reset_task is not None and not reset_task.done():
                reset_task.cancel()
            if request_task is not None and not request_task.done():
                request_task.cancel()
            await asyncio.gather(
                *(task for task in (reset_task, request_task) if task is not None),
                return_exceptions=True,
            )
            bus.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_session_reset_rejects_new_bus_request_and_drains_new_output() -> None:
    """Reset-time events settle before the EventBus and UI gates reopen.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Emit output and a UiRequest from Manager reload during application reset.

        Returns:
            None.
        """
        bus = EventBus()
        ui = _QueueingUi()
        reset_output_seen = asyncio.Event()
        reset_request_task: asyncio.Task[str] | None = None

        async def record_output(message: str, markdown: bool = False) -> None:
            """Record the reset-time output delivered through the app consumer.

            Args:
                message: Output content.
                markdown: Whether the content is Markdown.

            Returns:
                None.
            """
            assert message == "during reset"
            assert not markdown
            reset_output_seen.set()

        class ReloadEmitter:
            """Schedule reset-time bus events from a synchronous Manager reload."""

            def reload(self) -> None:
                """Publish one output and one UI request during reset.

                Returns:
                    None.
                """
                nonlocal reset_request_task
                asyncio.create_task(bus.request_output("during reset"))
                reset_request_task = asyncio.create_task(
                    bus.request_permission("shell", "pwd", caller_agent_type="child"),
                )

        ui._write = record_output  # type: ignore[method-assign]
        app, _deps, _store = _build_reset_app(ui, bus, ReloadEmitter())
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")
        consumer = asyncio.create_task(app._consume_events())
        try:
            await asyncio.sleep(0)
            with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
                result = await app._reset_session(source="clear")

            assert result is new_agent
            assert reset_output_seen.is_set()
            assert reset_request_task is not None and reset_request_task.cancelled()
            assert ui.accepted == []
        finally:
            if reset_request_task is not None and not reset_request_task.done():
                reset_request_task.cancel()
            if reset_request_task is not None:
                await asyncio.gather(reset_request_task, return_exceptions=True)
            bus.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_session_reset_drains_new_output_when_reload_raises() -> None:
    """The final EventBus drain runs before reset gates reopen on failure.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Emit output, fail Manager reload, then verify drain and gate restoration.

        Returns:
            None.
        """
        bus = EventBus()
        ui = _QueueingUi()
        reset_output_seen = asyncio.Event()

        async def record_output(message: str, markdown: bool = False) -> None:
            """Record output scheduled before the reset failure.

            Args:
                message: Output content.
                markdown: Whether the content is Markdown.

            Returns:
                None.
            """
            assert message == "before failure"
            assert not markdown
            reset_output_seen.set()

        class FailingReload:
            """Schedule one event and then abort shared-state mutation."""

            def reload(self) -> None:
                """Publish reset output and raise the expected reset failure.

                Returns:
                    None.

                Raises:
                    RuntimeError: Always, after scheduling output.
                """
                asyncio.create_task(bus.request_output("before failure"))
                raise RuntimeError("reload failed")

        ui._write = record_output  # type: ignore[method-assign]
        app, _deps, _store = _build_reset_app(ui, bus, FailingReload())
        consumer = asyncio.create_task(app._consume_events())
        next_request_task: asyncio.Task[str] | None = None
        try:
            await asyncio.sleep(0)
            try:
                await app._reset_session(source="clear")
            except RuntimeError as exc:
                assert str(exc) == "reload failed"
            else:
                raise AssertionError("reset failure was not propagated")

            assert reset_output_seen.is_set()
            next_request_task = asyncio.create_task(
                bus.request_permission("shell", "pwd", caller_agent_type="child"),
            )
            await asyncio.wait_for(bus.join(), timeout=1)

            assert not next_request_task.done()
            assert len(ui.accepted) == 1
        finally:
            if next_request_task is not None and not next_request_task.done():
                next_request_task.cancel()
            if next_request_task is not None:
                await asyncio.gather(next_request_task, return_exceptions=True)
            bus.close()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_session_reset_is_safe_with_an_empty_event_bus() -> None:
    """Startup-style reset does not require a running EventBus consumer.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Reset with a real bus that has no subscription or queued work.

        Returns:
            None.
        """
        bus = EventBus()
        ui = _QueueingUi()
        app, _deps, _store = _build_reset_app(ui, bus, None)
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")

        with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
            result = await asyncio.wait_for(
                app._reset_session(source="startup"),
                timeout=1,
            )

        assert result is new_agent

    asyncio.run(scenario())


def test_closed_tty_scheduler_cancels_requests_without_serial_fallback() -> None:
    """Shutdown-owned requests do not reopen a serial reader after WindowManager closes."""

    async def scenario() -> None:
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        read_calls: list[MenuRequest] = []

        async def unexpected_reader(request: MenuRequest) -> str:
            """Record an invalid fallback reader invocation."""
            read_calls.append(request)
            return "unexpected"

        controller._read_menu_request = unexpected_reader  # type: ignore[method-assign]
        await controller._window_manager.close()
        request = _input()

        await controller.on_event(request)

        assert read_calls == []
        assert request.future is not None and request.future.cancelled()

    asyncio.run(scenario())


def test_interrupted_app_waits_for_window_runner_after_event_bus_join() -> None:
    """Interrupt cleanup does not return before asynchronous UI runners settle."""

    async def scenario() -> None:
        order: list[str] = []
        deps = SimpleNamespace(ui=_InterruptUi(order), event_bus=_InterruptBus(order))
        app = AgentApp(deps=deps, agent_view_store=AgentViewStore(), output_router=object())

        await app._handle_interrupted_turn()

        assert order == ["cancel", "output", "join", "idle"]

    asyncio.run(scenario())


def test_session_reset_drains_ui_before_reloading_managers() -> None:
    """A new session starts only after active and queued UI requests are cleaned.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Run a reset with active and queued requests.

        Returns:
            None.
        """
        store = AgentViewStore()
        controller = InlineController(store)
        controller._tty = True
        controller.is_tty = True
        dialog_started = asyncio.Event()
        order: list[str] = []

        async def read_menu(_request: MenuRequest) -> str:
            """Hold the active dialog and record cancellation cleanup.

            Args:
                _request: Active request owned by the dialog runner.

            Returns:
                Dialog result; this runner is expected to be cancelled first.
            """
            dialog_started.set()
            try:
                await asyncio.Future()
            finally:
                order.append("dialog_cleanup")

        class ReloadProbe:
            """Assert that Manager reload begins only after UI cleanup."""

            def reload(self) -> None:
                """Record a reload after checking the window lifecycle barrier.

                Returns:
                    None.
                """
                assert controller._window_manager.is_idle
                assert controller._window_manager.window_stack == ()
                order.append("manager_reload")

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        active = _input()
        queued = _permission(caller="child")
        await controller.on_event(active)
        await controller.on_event(queued)
        await asyncio.wait_for(dialog_started.wait(), timeout=1)

        deps = SimpleNamespace(
            ui=controller,
            session_id="old-session",
            session_context=["old context"],
            memory_mgr=ReloadProbe(),
            tools_mgr=None,
            permission_mgr=None,
            config_mgr=None,
            plugin_mgr=None,
            hooks_mgr=None,
            plan_mgr=None,
            role_mgr=None,
            event_bus=None,
            plan_mode_controller=None,
        )
        app = AgentApp(deps=deps, agent_view_store=store, output_router=object())
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")

        try:
            with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
                result = await app._reset_session(source="clear")
        finally:
            await controller._window_manager.close()

        assert result is new_agent
        assert order == ["dialog_cleanup", "manager_reload"]
        assert active.future is not None and active.future.cancelled()
        assert queued.future is not None and queued.future.cancelled()
        assert controller._window_manager.is_idle
        assert controller._window_manager.window_stack == ()

    asyncio.run(scenario())


def test_session_reset_rejects_ui_request_arriving_during_cleanup() -> None:
    """A request delivered while old UI cleanup runs never enters the new session.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Deliver a permission request while reset waits for dialog cleanup.

        Returns:
            None.
        """
        store = AgentViewStore()
        controller = InlineController(store)
        controller._tty = True
        controller.is_tty = True
        active_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        started: list[MenuRequest] = []
        active = _input()

        async def read_menu(request: MenuRequest) -> str:
            """Pause cancelled-dialog cleanup and record every started request.

            Args:
                request: Request promoted by WindowManager.

            Returns:
                ``yes`` for any unexpected late request that starts.
            """
            started.append(request)
            if request is active:
                active_started.set()
                try:
                    await asyncio.Future()
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()
            return "yes"

        class ReloadProbe:
            """Assert that reload observes a fully drained window manager."""

            def reload(self) -> None:
                """Check the reset barrier before recording Manager reload.

                Returns:
                    None.
                """
                assert controller._window_manager.is_idle
                assert controller._window_manager.pending_summary == (0, None)
                assert controller._window_manager.window_stack == ()

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        await controller.on_event(active)
        await asyncio.wait_for(active_started.wait(), timeout=1)

        deps = SimpleNamespace(
            ui=controller,
            session_id="old-session",
            session_context=["old context"],
            memory_mgr=ReloadProbe(),
            tools_mgr=None,
            permission_mgr=None,
            config_mgr=None,
            plugin_mgr=None,
            hooks_mgr=None,
            plan_mgr=None,
            role_mgr=None,
            event_bus=None,
            plan_mode_controller=None,
        )
        app = AgentApp(deps=deps, agent_view_store=store, output_router=object())
        new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")
        reset_task: asyncio.Task[object] | None = None

        try:
            with patch("src.app.app.Agent.from_manifest", return_value=new_agent):
                reset_task = asyncio.create_task(app._reset_session(source="clear"))
                await asyncio.wait_for(cleanup_started.wait(), timeout=1)

                late = _permission(caller="child")
                await controller.on_event(late)
                await asyncio.sleep(0)

                assert late.future is not None and late.future.cancelled()
                assert started == [active]

                release_cleanup.set()
                result = await asyncio.wait_for(reset_task, timeout=1)

            assert result is new_agent
            assert active.future is not None and active.future.cancelled()
            assert controller._window_manager.is_idle
            assert controller._window_manager.pending_summary == (0, None)
            assert controller._window_manager.window_stack == ()
        finally:
            release_cleanup.set()
            if reset_task is not None and not reset_task.done():
                reset_task.cancel()
            if reset_task is not None:
                await asyncio.gather(reset_task, return_exceptions=True)
            await controller._window_manager.close()

    asyncio.run(scenario())


def test_session_reset_gate_reopens_after_exception() -> None:
    """A reset failure does not leave later UI requests rejected.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Raise inside the reset gate, then submit a normal request.

        Returns:
            None.
        """
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        started: list[MenuRequest] = []

        async def read_menu(request: MenuRequest) -> str:
            """Record and complete a request submitted after reset failure.

            Args:
                request: Request promoted after the reset gate exits.

            Returns:
                Successful permission result.
            """
            started.append(request)
            return "yes"

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        try:
            try:
                async with controller.reset_session_interactions():
                    raise RuntimeError("reset failed")
            except RuntimeError as exc:
                assert str(exc) == "reset failed"
            else:
                raise AssertionError("reset failure was not propagated")

            request = _permission(caller="child")
            await controller.on_event(request)
            await controller.wait_interactions_idle()

            assert request.future is not None and not request.future.cancelled()
            assert request.future.result() == "yes"
            assert started == [request]
        finally:
            await controller._window_manager.close()

    asyncio.run(scenario())


def test_session_reset_gate_reopens_after_cancellation() -> None:
    """Cancelling reset does not leave later UI requests rejected.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Cancel a task inside the reset gate, then submit a normal request.

        Returns:
            None.
        """
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        gate_entered = asyncio.Event()
        started: list[MenuRequest] = []

        async def read_menu(request: MenuRequest) -> str:
            """Record and complete a request submitted after reset cancellation.

            Args:
                request: Request promoted after the reset gate exits.

            Returns:
                Successful permission result.
            """
            started.append(request)
            return "yes"

        async def hold_reset_gate() -> None:
            """Wait indefinitely inside the reset gate until cancelled.

            Returns:
                None.
            """
            async with controller.reset_session_interactions():
                gate_entered.set()
                await asyncio.Future()

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        reset_task = asyncio.create_task(hold_reset_gate())
        try:
            await asyncio.wait_for(gate_entered.wait(), timeout=1)
            reset_task.cancel()
            await asyncio.gather(reset_task, return_exceptions=True)

            request = _permission(caller="child")
            await controller.on_event(request)
            await controller.wait_interactions_idle()

            assert request.future is not None and not request.future.cancelled()
            assert request.future.result() == "yes"
            assert started == [request]
        finally:
            if not reset_task.done():
                reset_task.cancel()
            await asyncio.gather(reset_task, return_exceptions=True)
            await controller._window_manager.close()

    asyncio.run(scenario())


def test_tty_controller_allows_permission_over_a_pending_transcript_view() -> None:
    """A transcript request no longer blocks a later permission dialog or telemetry."""

    async def scenario() -> None:
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        gate = asyncio.get_running_loop().create_future()
        started: list[MenuRequest] = []

        async def read_menu(request: MenuRequest) -> str:
            started.append(request)
            return await gate

        controller._read_menu_request = read_menu  # type: ignore[method-assign]
        view = _transcript("child-tty")
        permission = _permission(caller="child")

        await controller.on_event(view)
        await controller.on_event(permission)
        await asyncio.sleep(0)

        assert view.future is not None and not view.future.done()
        assert started == [permission]
        assert controller._top_window_kind == "permission"
        assert not controller._transcript_visible
        assert not controller._buffer_editable()
        assert controller._runtime.pending_input_future() is None

        controller._set_activity("思考中")
        assert controller._top_window_kind == "permission"

        gate.set_result("yes")
        await controller.wait_interactions_idle()

        assert permission.future is not None and permission.future.result() == "yes"
        assert controller._top_window_kind == "transcript"
        assert controller._viewing_uuid == "child-tty"
        assert controller._window_manager.close_transcript()
        assert view.future is not None and view.future.result() == ""

    asyncio.run(scenario())


def test_tty_pipe_routes_permission_keys_over_transcript() -> None:
    """A real prompt-toolkit key press reaches permission before the transcript."""

    async def scenario() -> None:
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        with create_pipe_input() as pipe_input:
            app = controller._build_application(input=pipe_input, output=DummyOutput())
            controller._app = app
            app_task = asyncio.create_task(app.run_async(handle_sigint=False))
            controller._app_task = app_task
            try:
                for _ in range(20):
                    if app.is_running:
                        break
                    await asyncio.sleep(0)
                assert app.is_running, app_task.exception() if app_task.done() else None

                view = _transcript("child-pipe")
                permission = _permission(caller="child")
                await controller.on_event(view)
                await controller.on_event(permission)
                await asyncio.sleep(0.1)

                assert app.is_running, app_task.exception() if app_task.done() else None
                assert controller._top_window_kind == "permission"
                assert controller._select_options is not None

                pipe_input.send_text("1")
                assert permission.future is not None
                await asyncio.sleep(0.1)
                assert permission.future.done()
                assert permission.future.result() == "yes"
                await controller.wait_interactions_idle()

                assert controller._transcript_visible
                form = _form()
                await controller.on_event(form)
                await asyncio.sleep(0.1)
                assert controller._top_window_kind == "form"
                pipe_input.send_text("\x1b")
                assert form.future is not None
                assert await asyncio.wait_for(form.future, timeout=1) == ""
                await controller.wait_interactions_idle()

                assert controller._transcript_visible
                pipe_input.send_text("\x1b")
                assert view.future is not None
                assert await asyncio.wait_for(view.future, timeout=1) == ""
            finally:
                await controller._window_manager.close()
                if app.is_running:
                    app.exit()
                await asyncio.gather(app_task, return_exceptions=True)

    asyncio.run(scenario())


def test_tty_pipe_routes_each_answer_window_kind() -> None:
    """Input, choice, form, and choice-input each retain their own key handling."""

    async def scenario() -> None:
        controller = InlineController(AgentViewStore())
        controller._tty = True
        controller.is_tty = True
        with create_pipe_input() as pipe_input:
            app = controller._build_application(input=pipe_input, output=DummyOutput())
            controller._app = app
            app_task = asyncio.create_task(app.run_async(handle_sigint=False))
            controller._app_task = app_task
            try:
                for _ in range(20):
                    if app.is_running:
                        break
                    await asyncio.sleep(0)
                assert app.is_running, app_task.exception() if app_task.done() else None

                input_request = _input()
                await controller.on_event(input_request)
                await asyncio.sleep(0.05)
                assert controller._top_window_kind == "input"
                assert controller._buffer_editable()
                pipe_input.send_text("draft")
                await asyncio.sleep(0.05)
                assert controller._buffer is not None
                saved_text = controller._buffer.text
                saved_cursor = controller._buffer.cursor_position
                controller._window_manager.open_live_transcript("child-input")
                assert controller._top_window_kind == "transcript"
                assert not controller._buffer_editable()
                pipe_input.send_text("\x1b")
                await asyncio.sleep(0.1)
                assert controller._top_window_kind == "input"
                assert controller._buffer.text == saved_text
                assert controller._buffer.cursor_position == saved_cursor
                pipe_input.send_text("\r")
                assert input_request.future is not None
                assert await asyncio.wait_for(input_request.future, timeout=1) == "draft"
                await controller.wait_interactions_idle()

                choice = _choice()
                await controller.on_event(choice)
                await asyncio.sleep(0.05)
                assert controller._top_window_kind == "choice"
                assert not controller._buffer_editable()
                pipe_input.send_text("2")
                assert choice.future is not None
                assert await asyncio.wait_for(choice.future, timeout=1) == "two"
                await controller.wait_interactions_idle()

                form = _form()
                await controller.on_event(form)
                await asyncio.sleep(0.05)
                assert controller._top_window_kind == "form"
                assert controller._form_answering()
                pipe_input.send_text("\x1b")
                assert form.future is not None
                assert await asyncio.wait_for(form.future, timeout=1) == ""
                await controller.wait_interactions_idle()

                choice_input = _choice_input()
                await controller.on_event(choice_input)
                await asyncio.sleep(0.05)
                assert controller._top_window_kind == "choice_input"
                pipe_input.send_text("1")
                assert choice_input.future is not None
                raw = await asyncio.wait_for(choice_input.future, timeout=1)
                assert json.loads(raw) == {"choice": "one", "text": ""}
                await controller.wait_interactions_idle()
            finally:
                await controller._window_manager.close()
                if app.is_running:
                    app.exit()
                await asyncio.gather(app_task, return_exceptions=True)

    asyncio.run(scenario())


def test_dialog_overlays_and_restores_live_transcript_state() -> None:
    """A dialog covers a live transcript without losing its UUID or scroll offset."""

    async def scenario() -> None:
        gate: asyncio.Future[str] | None = None

        async def run_dialog(_request: object) -> str:
            assert gate is not None
            return await gate

        manager = WindowManager(run_dialog)
        manager.open_live_transcript("child-1")
        manager.set_transcript_scroll(7)
        permission = _permission(caller="child")
        gate = asyncio.get_running_loop().create_future()

        manager.submit(permission)
        await asyncio.sleep(0)

        assert [entry.kind for entry in manager.window_stack] == ["transcript", "permission"]
        assert manager.transcript_uuid == "child-1"
        assert manager.transcript_scroll == 7

        gate.set_result("deny")
        await manager.wait_idle()

        assert [entry.kind for entry in manager.window_stack] == ["transcript"]
        assert manager.transcript_uuid == "child-1"
        assert manager.transcript_scroll == 7

    asyncio.run(scenario())


def test_live_transcript_covers_and_restores_normal_input() -> None:
    """Opening a live transcript pauses normal input without replacing its runner."""

    async def scenario() -> None:
        gate: asyncio.Future[str] | None = None

        async def run_dialog(_request: object) -> str:
            assert gate is not None
            return await gate

        manager = WindowManager(run_dialog)
        input_request = _input()
        gate = asyncio.get_running_loop().create_future()

        manager.submit(input_request)
        await asyncio.sleep(0)
        manager.open_live_transcript("child-input")

        assert [entry.kind for entry in manager.window_stack] == ["input", "transcript"]
        assert manager.active_window is not None
        assert manager.active_window.request is input_request
        assert manager.top_window is not None and manager.top_window.kind == "transcript"

        assert manager.close_transcript()
        assert manager.top_window is not None and manager.top_window.kind == "input"

        gate.set_result("resume")
        await manager.wait_idle()

    asyncio.run(scenario())


def test_view_request_waits_under_dialog_and_completes_after_close() -> None:
    """A transcript request stays open while a later dialog receives the keyboard."""

    async def scenario() -> None:
        gate: asyncio.Future[str] | None = None
        run_requests: list[object] = []

        async def run_dialog(request: object) -> str:
            run_requests.append(request)
            assert gate is not None
            return await gate

        manager = WindowManager(run_dialog)
        view = _transcript("child-2")
        permission = _permission(caller="child")
        gate = asyncio.get_running_loop().create_future()

        manager.submit(view)
        manager.submit(permission)
        await asyncio.sleep(0)

        assert run_requests == [permission]
        assert view.future is not None and not view.future.done()
        assert [entry.kind for entry in manager.window_stack] == ["transcript", "permission"]

        gate.set_result("yes")
        await manager.wait_idle()

        assert [entry.kind for entry in manager.window_stack] == ["transcript"]
        assert manager.close_transcript()
        assert view.future is not None and view.future.result() == ""

    asyncio.run(scenario())


def test_external_cancellation_and_close_leave_no_runner_or_future() -> None:
    """Caller cancellation and shutdown clear active, queued, and view requests.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Cancel one active caller, observe FIFO promotion, and close the manager.

        Returns:
            None.
        """
        started = asyncio.Event()
        released = asyncio.Event()

        async def run_dialog(_request: object) -> str:
            started.set()
            await released.wait()
            return "unreachable"

        manager = WindowManager(run_dialog)
        active = _input()
        queued = _permission(caller="child")
        view = _transcript("child-3")

        manager.submit(view)
        manager.submit(active)
        manager.submit(queued)
        await asyncio.wait_for(started.wait(), timeout=1)

        assert active.future is not None
        active.future.cancel()
        for _ in range(20):
            if manager.active_window is not None and manager.active_window.request is queued:
                break
            await asyncio.sleep(0)

        assert queued.future is not None and not queued.future.done()
        assert manager.active_window is not None
        assert manager.active_window.request is queued

        await manager.close()

        assert queued.future is not None and queued.future.cancelled()
        assert view.future is not None and view.future.cancelled()
        assert manager.window_stack == ()
        assert manager.is_idle

    asyncio.run(scenario())


def test_cancelled_queue_item_is_never_promoted_to_a_window() -> None:
    """A caller that stops waiting while queued never opens a stale dialog."""

    async def scenario() -> None:
        first_gate = asyncio.get_running_loop().create_future()
        started: list[MenuRequest] = []

        async def run_dialog(request: MenuRequest) -> str:
            started.append(request)
            return await first_gate

        manager = WindowManager(run_dialog)
        active = _input()
        cancelled = _permission(caller="child")

        manager.submit(active)
        await asyncio.sleep(0)
        manager.submit(cancelled)
        assert cancelled.future is not None
        cancelled.future.cancel()
        await asyncio.sleep(0)

        first_gate.set_result("done")
        await manager.wait_idle()

        assert started == [active]
        assert manager.window_stack == ()

    asyncio.run(scenario())


def test_cancel_all_reports_and_clears_a_live_transcript() -> None:
    """A no-future live transcript still counts as cancelled retained UI state."""

    async def run_dialog(_request: MenuRequest) -> str:
        """Provide an unused dialog runner for the manager under test."""
        return "unused"

    manager = WindowManager(run_dialog)
    manager.open_live_transcript("child-live")

    assert manager.cancel_all()
    assert manager.window_stack == ()


def test_cancel_all_waits_for_old_runner_cleanup_before_starting_new_dialog() -> None:
    """A new dialog cannot overlap the cancelled runner's shared UI cleanup."""

    async def scenario() -> None:
        active_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        never = asyncio.get_running_loop().create_future()
        started: list[MenuRequest] = []
        first = _input()
        second = _permission(caller="child")

        async def run_dialog(request: MenuRequest) -> str:
            """Delay first-runner cleanup to expose any premature FIFO promotion."""
            started.append(request)
            if request is first:
                active_started.set()
                try:
                    await never
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()
            return "yes"

        manager = WindowManager(run_dialog)
        manager.submit(first)
        await asyncio.wait_for(active_started.wait(), timeout=1)

        assert manager.cancel_all()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        manager.submit(second)
        await asyncio.sleep(0)

        assert started == [first]
        assert manager.pending_summary == (1, "child")

        release_cleanup.set()
        await manager.wait_idle()

        assert started == [first, second]
        assert second.future is not None and second.future.result() == "yes"

    asyncio.run(scenario())


def test_cancelled_unstarted_runner_releases_its_window_entry() -> None:
    """A task cancelled before its coroutine starts cannot strand FIFO state."""

    async def scenario() -> None:
        started: list[MenuRequest] = []

        async def run_dialog(request: MenuRequest) -> str:
            """Record the only dialog runner that is allowed to start."""
            started.append(request)
            return "yes"

        manager = WindowManager(run_dialog)
        cancelled = _input()
        next_request = _permission(caller="child")

        manager.submit(cancelled)
        runner = manager._runner
        assert runner is not None
        runner.cancel()
        manager.submit(next_request)
        await manager.wait_idle()

        assert cancelled.future is not None and cancelled.future.cancelled()
        assert started == [next_request]
        assert next_request.future is not None and next_request.future.result() == "yes"

    asyncio.run(scenario())


def test_is_idle_cannot_skip_unstarted_runner_cleanup() -> None:
    """An idle probe cannot make the cleanup barrier forget a done runner."""

    async def scenario() -> None:
        started: list[MenuRequest] = []

        async def run_dialog(request: MenuRequest) -> str:
            """Record an invalid runner start for the cancelled request."""
            started.append(request)
            return "unreachable"

        manager = WindowManager(run_dialog)
        request = _input()

        manager.submit(request)
        runner = manager._runner
        assert runner is not None
        runner.cancel()
        await asyncio.sleep(0)

        assert runner.done()
        assert not manager.is_idle
        await manager.wait_idle()

        assert started == []
        assert request.future is not None and request.future.cancelled()
        assert manager.window_stack == ()
        assert manager._runner is None
        assert manager.is_idle

    asyncio.run(scenario())


def test_wait_idle_cannot_finish_before_started_runner_done_callback() -> None:
    """A normal runner remains busy until manager done-callback cleanup finishes.

    Returns:
        None.
    """

    async def scenario() -> None:
        """Start an idle waiter from the runner's terminal UI notification.

        Returns:
            None.
        """
        release_dialog = asyncio.Event()
        runner_started = asyncio.Event()
        waiter_started = asyncio.Event()
        callback_finished = False
        waiter_observation: list[bool] = []
        waiter: asyncio.Task[None] | None = None
        manager: WindowManager

        async def run_dialog(_request: MenuRequest) -> str:
            """Wait until the test permits the normally started runner to finish.

            Args:
                _request: Active request owned by the runner.

            Returns:
                Successful dialog answer.
            """
            runner_started.set()
            await release_dialog.wait()
            return "done"

        async def observe_idle() -> None:
            """Record whether done-callback cleanup preceded idle completion.

            Returns:
                None.
            """
            waiter_started.set()
            await manager.wait_idle()
            waiter_observation.append(callback_finished)

        def on_change() -> None:
            """Start the waiter when the dialog entry first disappears.

            Returns:
                None.
            """
            nonlocal waiter
            if runner_started.is_set() and manager.active_window is None and waiter is None:
                waiter = asyncio.create_task(observe_idle())

        manager = WindowManager(run_dialog, on_change)
        original_forget_runner = manager._forget_runner

        def record_forget_runner(
            runner: asyncio.Task[DialogOutcome],
            entry: WindowEntry,
        ) -> None:
            """Record completion after delegating manager runner cleanup.

            Args:
                runner: Completed dialog task.
                entry: Window entry owned by the task.

            Returns:
                None.
            """
            nonlocal callback_finished
            original_forget_runner(runner, entry)
            callback_finished = True

        manager._forget_runner = record_forget_runner  # type: ignore[method-assign]
        request = _input()
        manager.submit(request)
        await asyncio.wait_for(runner_started.wait(), timeout=1)

        release_dialog.set()
        await asyncio.wait_for(waiter_started.wait(), timeout=1)
        assert waiter is not None
        await asyncio.wait_for(waiter, timeout=1)

        assert request.future is not None and request.future.result() == "done"
        assert waiter_observation == [True]
        assert manager.is_idle

    asyncio.run(scenario())


def test_runner_failure_settles_the_request_and_starts_the_next_dialog() -> None:
    """A failed dialog runner does not strand later FIFO requests."""

    async def scenario() -> None:
        gate = asyncio.get_running_loop().create_future()
        started: list[MenuRequest] = []

        async def run_dialog(request: MenuRequest) -> str:
            started.append(request)
            if len(started) == 1:
                raise RuntimeError("runner failed")
            return await gate

        manager = WindowManager(run_dialog)
        failed = _input()
        next_request = _permission(caller="child")

        manager.submit(failed)
        manager.submit(next_request)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert failed.future is not None
        assert isinstance(failed.future.exception(), RuntimeError)
        assert manager.active_window is not None
        assert manager.active_window.request is next_request

        gate.set_result("yes")
        await manager.wait_idle()

        assert next_request.future is not None and next_request.future.result() == "yes"

    asyncio.run(scenario())
