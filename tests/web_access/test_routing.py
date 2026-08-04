from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.mgr.llm_mgr import LLMMgr, _normalize_provider_configs
from src.mgr.web_access_mgr import WebAccessMgr
from src.web.types import (
    NativeWebCapabilityError,
    WebFetchResponse,
    WebSearchResponse,
    WebSource,
)


class FakeLLMMgr:
    def __init__(self, mode: str = "provider") -> None:
        self.mode = mode

    def web_mode_for_model(self, _model: str) -> str:
        return self.mode

    def provider_name_for_model(self, _model: str) -> str:
        return "fake"


class FakeProvider:
    model = "fake-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.search_calls = 0
        self.fetch_calls = 0

    async def native_web_search(self, _query: str, *, max_results: int):
        self.search_calls += 1
        if self.error:
            raise self.error
        return WebSearchResponse("native", (WebSource("https://native.test"),))

    async def native_web_fetch(self, url: str):
        self.fetch_calls += 1
        if self.error:
            raise self.error
        return WebFetchResponse(url, url, "native")


def run(coro):
    return asyncio.run(coro)


def test_local_mode_never_calls_provider(monkeypatch):
    monkeypatch.setattr(
        "src.mgr.web_access_mgr.local_search",
        lambda query, max_results: WebSearchResponse("local"),
    )
    provider = FakeProvider()
    result = run(WebAccessMgr(FakeLLMMgr("local")).search(
        "query", max_results=3, provider=provider
    ))
    assert provider.search_calls == 0
    assert "路由: local" in result
    assert "local" in result


def test_provider_capability_error_falls_back_local(monkeypatch):
    monkeypatch.setattr(
        "src.mgr.web_access_mgr.local_fetch",
        lambda url: WebFetchResponse(url, url, "local"),
    )
    provider = FakeProvider(NativeWebCapabilityError("unsupported"))
    result = run(WebAccessMgr(FakeLLMMgr()).fetch(
        "https://example.test/", provider=provider
    ))
    assert provider.fetch_calls == 1
    assert "路由: local" in result
    assert "local" in result


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("protocol")])
def test_provider_operational_errors_do_not_fall_back(monkeypatch, error):
    called = False

    def local(_query, _max_results):
        nonlocal called
        called = True
        return WebSearchResponse("local")

    monkeypatch.setattr("src.mgr.web_access_mgr.local_search", local)
    provider = FakeProvider(error)
    with pytest.raises(type(error)):
        run(WebAccessMgr(FakeLLMMgr()).search(
            "query", max_results=3, provider=provider
        ))
    assert called is False


def test_provider_web_config_defaults_and_validation():
    base = {"openai": {"base_url": "https://api.example.test", "models": []}}
    assert _normalize_provider_configs(base)["openai"]["web"] == "local"
    base["openai"]["web"] = "provider"
    assert _normalize_provider_configs(base)["openai"]["web"] == "provider"
    base["openai"]["web"] = "search"
    with pytest.raises(Exception, match="web"):
        _normalize_provider_configs(base)


def test_llm_mgr_resolves_web_mode_from_model():
    config = {
        "llm": {
            "default": "model-a",
            "concurrency": 1,
            "timeout_seconds": 10,
            "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 1},
        },
        "llm_provider": {
            "openai": {
                "base_url": "https://api.example.test",
                "models": ["model-a"],
                "web": "provider",
            }
        },
        "tool.page_token_rate": 0.03,
    }

    class Config:
        def get_config(self, key):
            if key == "tool.page_token_rate":
                return config[key]
            return config[key]

    mgr = LLMMgr(Config(), event_bus=None)
    mgr._model_to_provider = {"model-a": "openai"}
    mgr._provider_web_mode = {"openai": "provider"}
    assert mgr.provider_name_for_model("model-a") == "openai"
    assert mgr.web_mode_for_model("model-a") == "provider"
