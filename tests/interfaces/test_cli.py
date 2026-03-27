"""Tests for CLIInterface."""
import pytest
from unittest.mock import patch, AsyncMock
from src.interfaces.base import UserInterface
from src.interfaces.cli import CLIInterface


class TestCLIInterface:

    def test_implements_protocol(self):
        cli = CLIInterface()
        assert isinstance(cli, UserInterface)

    @pytest.mark.asyncio
    async def test_display(self, capsys):
        cli = CLIInterface()
        await cli.display("hello world")
        captured = capsys.readouterr()
        assert captured.out == "hello world"

    @pytest.mark.asyncio
    async def test_prompt(self):
        cli = CLIInterface()
        with patch("asyncio.to_thread", new_callable=AsyncMock, return_value="user reply"):
            result = await cli.prompt("Enter: ")
            assert result == "user reply"

    @pytest.mark.asyncio
    async def test_confirm_yes(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="y"):
            assert await cli.confirm("Continue?") is True

    @pytest.mark.asyncio
    async def test_confirm_no(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="n"):
            assert await cli.confirm("Continue?") is False

    @pytest.mark.asyncio
    async def test_confirm_chinese(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="确认"):
            assert await cli.confirm("Continue?") is True
