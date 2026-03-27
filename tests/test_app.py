"""Tests for AgentApp."""
import pytest
from unittest.mock import AsyncMock, Mock
from src.app.app import AgentApp


def _make_mock_ui():
    ui = AsyncMock()
    ui.prompt = AsyncMock(return_value="exit")
    ui.display = AsyncMock()
    ui.confirm = AsyncMock(return_value=True)
    return ui


def _make_app(ui=None):
    """Create an AgentApp with all dependencies mocked."""
    if ui is None:
        ui = _make_mock_ui()
    return AgentApp(
        deps=Mock(),
        ui=ui,
        guardrail=Mock(check=Mock(return_value=(True, ""))),
        tool_router=Mock(),
        agent_registry=Mock(),
        engine=Mock(),
        graph=Mock(),
        skill_manager=Mock(is_slash_command=Mock(return_value=None)),
        mcp_manager=Mock(),
        runner=Mock(),
    )


class TestAgentAppProcess:

    @pytest.mark.asyncio
    async def test_guardrail_blocks_dangerous_input(self):
        ui = _make_mock_ui()
        app = _make_app(ui=ui)
        app.guardrail = Mock(check=Mock(return_value=(False, "不安全内容")))

        await app.process("rm -rf /")
        ui.display.assert_called()
        call_text = ui.display.call_args[0][0]
        assert "安全拦截" in call_text

    @pytest.mark.asyncio
    async def test_plan_command_no_request(self):
        ui = _make_mock_ui()
        app = _make_app(ui=ui)

        await app.process("/plan")
        ui.display.assert_called()
        call_text = ui.display.call_args[0][0]
        assert "/plan" in call_text


class TestAgentAppRun:

    @pytest.mark.asyncio
    async def test_exit_command_stops_loop(self):
        ui = _make_mock_ui()
        ui.prompt = AsyncMock(return_value="exit")
        app = _make_app(ui=ui)

        await app.run()
        # Should have displayed startup message and then exited
        ui.display.assert_called()
