"""UserInputToolProvider 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass

from src.tools.user_input import UserInputToolProvider


@pytest.fixture
def mock_interaction():
    interaction = AsyncMock()
    interaction.ask = AsyncMock(return_value="用户的回答")
    return interaction


@pytest.fixture
def provider(mock_interaction):
    return UserInputToolProvider(mock_interaction)


class TestCanHandle:
    def test_handles_ask_user(self, provider):
        assert provider.can_handle("ask_user") is True

    def test_rejects_other_tools(self, provider):
        assert provider.can_handle("get_weather") is False
        assert provider.can_handle("ask_user_v2") is False


class TestGetSchemas:
    def test_returns_ask_user_schema(self, provider):
        schemas = provider.get_schemas()
        assert len(schemas) == 1
        func = schemas[0]["function"]
        assert func["name"] == "ask_user"
        assert "question" in func["parameters"]["properties"]
        assert "question" in func["parameters"]["required"]


class TestExecute:
    @pytest.mark.asyncio
    async def test_returns_user_answer(self, provider, mock_interaction):
        result = await provider.execute("ask_user", {"question": "你想要什么？"})
        assert result == "用户的回答"
        mock_interaction.ask.assert_called_once_with("你想要什么？", source="")

    @pytest.mark.asyncio
    async def test_extracts_source_from_context(self, provider, mock_interaction):
        @dataclass
        class FakeContext:
            current_agent: str = "weather_agent"

        await provider.execute("ask_user", {"question": "城市？"}, context=FakeContext())
        mock_interaction.ask.assert_called_once_with("城市？", source="weather_agent")

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self, provider, mock_interaction):
        result = await provider.execute("ask_user", {"question": ""})
        assert "错误" in result
        mock_interaction.ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_question_returns_error(self, provider, mock_interaction):
        result = await provider.execute("ask_user", {})
        assert "错误" in result
        mock_interaction.ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_context_uses_empty_source(self, provider, mock_interaction):
        await provider.execute("ask_user", {"question": "问题"}, context=None)
        mock_interaction.ask.assert_called_once_with("问题", source="")
