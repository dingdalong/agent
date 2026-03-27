"""Tests for src.llm.types."""

from src.llm.types import LLMResponse, ToolCallData, StreamChunk


class TestLLMResponse:
    def test_default_values(self):
        resp = LLMResponse(content="hello")
        assert resp.content == "hello"
        assert resp.tool_calls == {}
        assert resp.finish_reason is None

    def test_with_tool_calls(self):
        tc = {0: {"id": "call_1", "name": "get_weather", "arguments": '{"city": "广州"}'}}
        resp = LLMResponse(content="", tool_calls=tc, finish_reason="tool_calls")
        assert resp.tool_calls[0]["name"] == "get_weather"
        assert resp.finish_reason == "tool_calls"


class TestToolCallData:
    def test_creation(self):
        tc = ToolCallData(id="1", name="test", arguments='{}')
        assert tc.id == "1"
        assert tc.name == "test"


class TestStreamChunk:
    def test_default_values(self):
        chunk = StreamChunk()
        assert chunk.content == ""
        assert chunk.tool_calls_delta == {}
        assert chunk.finish_reason is None
