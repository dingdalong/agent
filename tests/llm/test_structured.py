"""Tests for src.llm.structured — migrated from tests/core/test_async_api.py."""

from pydantic import BaseModel
from src.llm.structured import build_output_schema, parse_output


class SampleOutput(BaseModel):
    score: float
    label: str


class TestBuildOutputSchema:
    def test_generates_valid_schema(self):
        schema = build_output_schema("test", "desc", SampleOutput)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "test"
        assert "properties" in schema["function"]["parameters"]

    def test_strips_title_and_description(self):
        schema = build_output_schema("test", "desc", SampleOutput)
        params = schema["function"]["parameters"]
        assert "title" not in params
        assert "description" not in params


class TestParseOutput:
    def test_parses_matching_tool_call(self):
        tool_calls = {0: {"name": "test", "arguments": '{"score": 0.9, "label": "good"}'}}
        result = parse_output(tool_calls, "test", SampleOutput)
        assert result is not None
        assert result.score == 0.9
        assert result.label == "good"

    def test_returns_none_for_no_match(self):
        tool_calls = {0: {"name": "other", "arguments": "{}"}}
        assert parse_output(tool_calls, "test", SampleOutput) is None

    def test_returns_none_for_invalid_json(self):
        tool_calls = {0: {"name": "test", "arguments": "not json"}}
        assert parse_output(tool_calls, "test", SampleOutput) is None

    def test_returns_none_for_validation_error(self):
        tool_calls = {0: {"name": "test", "arguments": '{"wrong": "field"}'}}
        assert parse_output(tool_calls, "test", SampleOutput) is None
