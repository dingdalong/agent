"""Tests for src.llm.openai — OpenAIProvider."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from openai import APIConnectionError, RateLimitError

from src.llm.openai import OpenAIProvider
from src.llm.types import LLMResponse


# ---------------------------------------------------------------------------
# Helpers to build fake stream chunks
# ---------------------------------------------------------------------------

def _make_chunk(content=None, tool_calls=None, finish_reason=None):
    """Build a minimal fake OpenAI stream chunk."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls or []

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason

    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


def _make_tool_chunk(idx, tc_id=None, name=None, arguments=None):
    """Build a fake tool_calls delta item."""
    fn = MagicMock()
    fn.name = name or ""
    fn.arguments = arguments or ""

    tc = MagicMock()
    tc.index = idx
    tc.id = tc_id or ""
    tc.function = fn
    return tc


async def _async_iter(items):
    """Turn a list into an async iterator."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """Return an OpenAIProvider with a patched AsyncOpenAI client."""
    with patch("src.llm.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        p = OpenAIProvider(
            api_key="test-key",
            base_url="https://api.example.com",
            model="gpt-4o",
            concurrency=3,
            max_retries=3,
        )
        p._client = mock_client
        yield p


# ---------------------------------------------------------------------------
# 1. Constructor tests
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_model_is_set(self, provider):
        assert provider.model == "gpt-4o"

    def test_max_retries_is_set(self, provider):
        assert provider.max_retries == 3

    def test_semaphore_concurrency(self, provider):
        # asyncio.Semaphore stores its value as _value
        assert provider._semaphore._value == 3

    def test_on_chunk_default_none(self, provider):
        assert provider._on_chunk is None

    def test_on_chunk_stored(self):
        cb = AsyncMock()
        with patch("src.llm.openai.AsyncOpenAI"):
            p = OpenAIProvider(
                api_key="k", base_url="u", model="m", on_chunk=cb
            )
        assert p._on_chunk is cb


# ---------------------------------------------------------------------------
# 2. chat() — correct params forwarded to the SDK
# ---------------------------------------------------------------------------

class TestChat:
    @pytest.mark.asyncio
    async def test_chat_calls_create_with_correct_params(self, provider):
        messages = [{"role": "user", "content": "hello"}]
        chunks = [
            _make_chunk(content="Hi"),
            _make_chunk(finish_reason="stop"),
        ]

        mock_create = AsyncMock(return_value=_async_iter(chunks))
        provider._client.chat.completions.create = mock_create

        await provider.chat(messages=messages, temperature=0.5)

        mock_create.assert_called_once_with(
            model="gpt-4o",
            messages=messages,
            tools=None,
            stream=True,
            temperature=0.5,
            tool_choice=None,
        )

    @pytest.mark.asyncio
    async def test_chat_with_tools_sets_tool_choice_auto(self, provider):
        tools = [{"type": "function", "function": {"name": "search"}}]
        chunks = [_make_chunk(finish_reason="tool_calls")]

        mock_create = AsyncMock(return_value=_async_iter(chunks))
        provider._client.chat.completions.create = mock_create

        await provider.chat(messages=[], tools=tools)

        _, kwargs = mock_create.call_args
        assert kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_chat_explicit_tool_choice_overrides_auto(self, provider):
        tools = [{"type": "function", "function": {"name": "search"}}]
        chunks = [_make_chunk(finish_reason="stop")]

        mock_create = AsyncMock(return_value=_async_iter(chunks))
        provider._client.chat.completions.create = mock_create

        await provider.chat(messages=[], tools=tools, tool_choice="none")

        _, kwargs = mock_create.call_args
        assert kwargs["tool_choice"] == "none"

    @pytest.mark.asyncio
    async def test_chat_returns_llm_response(self, provider):
        chunks = [
            _make_chunk(content="hello"),
            _make_chunk(finish_reason="stop"),
        ]
        mock_create = AsyncMock(return_value=_async_iter(chunks))
        provider._client.chat.completions.create = mock_create

        result = await provider.chat(messages=[])
        assert isinstance(result, LLMResponse)
        assert result.content == "hello"


# ---------------------------------------------------------------------------
# 3. _parse_stream() — content and tool_calls assembly
# ---------------------------------------------------------------------------

class TestParseStream:
    @pytest.mark.asyncio
    async def test_assembles_content_parts(self, provider):
        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(content=", "),
            _make_chunk(content="world"),
            _make_chunk(finish_reason="stop"),
        ]
        result = await provider._parse_stream(_async_iter(chunks))
        assert result.content == "Hello, world"

    @pytest.mark.asyncio
    async def test_finish_reason_captured(self, provider):
        chunks = [_make_chunk(finish_reason="stop")]
        result = await provider._parse_stream(_async_iter(chunks))
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_assembles_tool_calls_across_chunks(self, provider):
        tc1a = _make_tool_chunk(idx=0, tc_id="call_1", name="search", arguments="")
        tc1b = _make_tool_chunk(idx=0, arguments='{"q":')
        tc1c = _make_tool_chunk(idx=0, arguments='"cats"}')

        chunks = [
            _make_chunk(tool_calls=[tc1a]),
            _make_chunk(tool_calls=[tc1b]),
            _make_chunk(tool_calls=[tc1c]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        result = await provider._parse_stream(_async_iter(chunks))

        assert 0 in result.tool_calls
        tc = result.tool_calls[0]
        assert tc["id"] == "call_1"
        assert tc["name"] == "search"
        assert tc["arguments"] == '{"q":"cats"}'

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_tracked_by_index(self, provider):
        tc0 = _make_tool_chunk(idx=0, tc_id="id0", name="fn_a", arguments='{"x":1}')
        tc1 = _make_tool_chunk(idx=1, tc_id="id1", name="fn_b", arguments='{"y":2}')

        chunks = [
            _make_chunk(tool_calls=[tc0, tc1]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        result = await provider._parse_stream(_async_iter(chunks))

        assert result.tool_calls[0]["name"] == "fn_a"
        assert result.tool_calls[1]["name"] == "fn_b"

    @pytest.mark.asyncio
    async def test_whitespace_content_alongside_tool_calls_is_skipped(self, provider):
        """Content that is only whitespace when tool_calls are present is skipped."""
        tc = _make_tool_chunk(idx=0, tc_id="id0", name="fn", arguments="{}")
        chunks = [
            _make_chunk(content="   ", tool_calls=[tc]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        result = await provider._parse_stream(_async_iter(chunks))
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_empty_stream_returns_empty_response(self, provider):
        result = await provider._parse_stream(_async_iter([]))
        assert result.content == ""
        assert result.tool_calls == {}
        assert result.finish_reason is None


# ---------------------------------------------------------------------------
# 4. Retry logic
# ---------------------------------------------------------------------------

class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_api_connection_error(self, provider):
        chunks = [_make_chunk(content="ok"), _make_chunk(finish_reason="stop")]

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise APIConnectionError(request=MagicMock())
            return _async_iter(chunks)

        provider._client.chat.completions.create = side_effect

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await provider.chat(messages=[])

        assert call_count == 3
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_error(self, provider):
        chunks = [_make_chunk(finish_reason="stop")]

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                )
            return _async_iter(chunks)

        provider._client.chat.completions.create = side_effect

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await provider.chat(messages=[])

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exceeded(self, provider):
        async def always_fail(**kwargs):
            raise APIConnectionError(request=MagicMock())

        provider._client.chat.completions.create = always_fail

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(APIConnectionError):
                await provider.chat(messages=[])

    @pytest.mark.asyncio
    async def test_sleep_uses_exponential_backoff(self, provider):
        chunks = [_make_chunk(finish_reason="stop")]
        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise APIConnectionError(request=MagicMock())
            return _async_iter(chunks)

        provider._client.chat.completions.create = side_effect

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await provider.chat(messages=[])

        # attempt 0 → sleep(1), attempt 1 → sleep(2)
        assert mock_sleep.call_args_list == [call(1), call(2)]


# ---------------------------------------------------------------------------
# 5. on_chunk callback
# ---------------------------------------------------------------------------

class TestOnChunkCallback:
    @pytest.mark.asyncio
    async def test_on_chunk_called_during_streaming(self):
        cb = AsyncMock()
        with patch("src.llm.openai.AsyncOpenAI"):
            p = OpenAIProvider(
                api_key="k", base_url="u", model="m", on_chunk=cb
            )

        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(content=" there"),
            _make_chunk(finish_reason="stop"),
        ]
        mock_create = AsyncMock(return_value=_async_iter(chunks))
        p._client.chat.completions.create = mock_create

        await p.chat(messages=[])

        assert cb.call_count == 2
        cb.assert_any_call("Hello")
        cb.assert_any_call(" there")

    @pytest.mark.asyncio
    async def test_on_chunk_not_called_when_silent(self):
        cb = AsyncMock()
        with patch("src.llm.openai.AsyncOpenAI"):
            p = OpenAIProvider(
                api_key="k", base_url="u", model="m", on_chunk=cb
            )

        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(finish_reason="stop"),
        ]
        mock_create = AsyncMock(return_value=_async_iter(chunks))
        p._client.chat.completions.create = mock_create

        await p.chat(messages=[], silent=True)

        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_callback_registered_no_error(self, provider):
        """Provider without on_chunk should not raise even when content arrives."""
        chunks = [
            _make_chunk(content="text"),
            _make_chunk(finish_reason="stop"),
        ]
        mock_create = AsyncMock(return_value=_async_iter(chunks))
        provider._client.chat.completions.create = mock_create

        result = await provider.chat(messages=[])
        assert result.content == "text"
