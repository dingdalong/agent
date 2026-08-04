from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.llm.anthropic import AnthropicProvider
from src.llm.deepseek import DeepSeekProvider
from src.llm.openai import OpenAIProvider
from src.web.types import NativeWebCapabilityError


def run(coro):
    return asyncio.run(coro)


def openai_provider() -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="gpt-test",
        event_bus=None,
        max_attempts=1,
    )


def anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="claude-test",
        event_bus=None,
        max_attempts=1,
        max_pause_turn_continuations=1,
    )


def test_openai_native_search_is_isolated_and_bounded():
    provider = openai_provider()
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            output_text="answer",
            output=[{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "answer",
                    "annotations": [{
                        "type": "url_citation",
                        "url": "https://source.test/",
                        "title": "source",
                    }],
                }],
            }],
            usage=None,
        )

    provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    response = run(provider.native_web_search("query", max_results=3))
    request = requests[0]
    assert response.summary == "answer"
    assert response.sources[0].url == "https://source.test/"
    assert request["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert request["tool_choice"] == "required"
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["max_tool_calls"] == 1
    assert request["store"] is False
    assert len(request["input"]) == 1 and request["input"][0]["role"] == "user"
    assert "query" in request["input"][0]["content"]
    assert "messages" not in request and "previous_response_id" not in request


def test_openai_fetch_and_deepseek_fetch_are_capability_errors():
    openai = openai_provider()
    deepseek = DeepSeekProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="deepseek-test",
        event_bus=None,
        max_attempts=1,
    )
    with pytest.raises(NativeWebCapabilityError):
        run(openai.native_web_fetch("https://example.test/"))
    with pytest.raises(NativeWebCapabilityError):
        run(deepseek.native_web_fetch("https://example.test/"))


def test_deepseek_native_search_is_isolated_and_bounded():
    provider = DeepSeekProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="deepseek-test",
        event_bus=None,
        max_attempts=1,
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            output_text="answer",
            output=[{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "answer",
                    "annotations": [{
                        "type": "url_citation",
                        "url": "https://source.test/",
                        "title": "source",
                    }],
                }],
            }],
            usage=None,
        )

    provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    response = run(provider.native_web_search("query", max_results=3))
    request = requests[0]
    assert response.summary == "answer"
    assert response.sources[0].url == "https://source.test/"
    assert request["tools"] == [{"type": "web_search"}]
    assert request["tool_choice"] == {"type": "web_search"}
    assert request["store"] is False
    assert len(request["input"]) == 1 and request["input"][0]["role"] == "user"
    assert "query" in request["input"][0]["content"]


def test_anthropic_native_search_uses_one_server_tool():
    provider = anthropic_provider()
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=None,
            content=[
                {"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {"query": "query"}},
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srv",
                    "content": [{
                        "type": "web_search_result",
                        "url": "https://source.test/",
                        "title": "source",
                        "encrypted_content": "opaque",
                    }],
                },
                {"type": "text", "text": "answer"},
            ],
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = run(provider.native_web_search("query"))
    assert response.summary == "answer"
    assert response.sources[0].url == "https://source.test/"
    assert requests[0]["tools"][0]["max_uses"] == 1
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert len(requests[0]["messages"]) == 1


def test_anthropic_native_fetch_validates_exact_url():
    provider = anthropic_provider()

    async def create(**_kwargs):
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=None,
            content=[
                {
                    "type": "server_tool_use",
                    "id": "srv",
                    "name": "web_fetch",
                    "input": {"url": "https://example.test/doc"},
                },
                {
                    "type": "web_fetch_tool_result",
                    "tool_use_id": "srv",
                    "content": {
                        "type": "web_fetch_result",
                        "url": "https://example.test/doc",
                        "content": {"type": "document", "source": {"type": "text", "data": "body"}},
                    },
                },
                {"type": "text", "text": "body"},
            ],
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = run(provider.native_web_fetch("https://example.test/doc"))
    assert response.final_url == "https://example.test/doc"
    assert response.content == "body"
