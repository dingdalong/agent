from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from src.tools import AccessKind, DataFlow, ToolOrigin, ToolPolicy
from src.events.types import ToolCallCompleted
from src.mgr.data_guard import DataGuard
from src.mgr.permission_mgr import JudgeVerdict, PermissionManager
from src.mgr.tools_mgr import ToolsMgr
from src.tools.decorator import ToolEntry


class WebReviewer:
    def __init__(self, verdict=JudgeVerdict("allow", "ok")) -> None:
        self.verdict = verdict
        self.calls = []

    async def review(self, request, *, model):
        self.calls.append((dict(request), model))
        return self.verdict


def run(coro):
    return asyncio.run(coro)


def policy() -> ToolPolicy:
    return ToolPolicy(AccessKind.EXTERNAL_READ, DataFlow.EXTERNAL)


def test_safe_web_fetch_is_allowed_by_privacy_precheck(tmp_path: Path):
    """隐私预检通过的安全 URL 应直接放行，不调用 LLM 审查。"""
    reviewer = WebReviewer()
    manager = PermissionManager(
        str(tmp_path), None, None, DataGuard(), web_safety_client=reviewer
    )
    result = run(manager.authorize(
        "web_fetch",
        policy(),
        {"url": "https://example.test/doc?lang=zh"},
        origin=ToolOrigin("builtin"),
        plan_active=True,
        user_intent="读取这份公开文档",
        review_model="current-model",
    ))
    assert result.allowed and result.source == "web_safety"
    assert reviewer.calls == []


def test_personal_search_skips_llm_and_asks_once(tmp_path: Path):
    reviewer = WebReviewer()
    confirmations = []

    async def confirm(tool_name: str, detail: str) -> bool:
        confirmations.append((tool_name, detail))
        return True

    manager = PermissionManager(
        str(tmp_path), None, confirm, DataGuard(), web_safety_client=reviewer
    )
    result = run(manager.authorize(
        "web_search",
        policy(),
        {"query": "alice@example.com recent profile", "max_results": 5},
        origin=ToolOrigin("builtin"),
        plan_active=True,
        user_intent="search",
        review_model="current-model",
    ))
    assert result.allowed and result.source == "user"
    assert reviewer.calls == []
    assert confirmations == [("web_search", "搜索网页：<query:length=32>")]


def test_secret_is_denied_before_web_reviewer(tmp_path: Path):
    secret = "sk-this-is-a-secret-value"
    reviewer = WebReviewer()
    manager = PermissionManager(
        str(tmp_path), None, None, DataGuard(), web_safety_client=reviewer
    )
    result = run(manager.authorize(
        "web_search",
        policy(),
        {"query": secret, "max_results": 5},
        origin=ToolOrigin("builtin"),
        plan_active=False,
        user_intent="search",
        review_model="current-model",
    ))
    assert not result.allowed and result.source == "hard_rule"
    assert secret not in result.reason
    assert reviewer.calls == []


def test_normal_long_search_does_not_trigger_false_private_identifier(tmp_path: Path):
    reviewer = WebReviewer()
    manager = PermissionManager(
        str(tmp_path), None, None, DataGuard(), web_safety_client=reviewer
    )
    result = run(manager.authorize(
        "web_search",
        policy(),
        {"query": "OpenAI Responses API web search documentation", "max_results": 5},
        origin=ToolOrigin("builtin"),
        plan_active=False,
        user_intent="research",
        review_model="current-model",
    ))
    assert result.allowed
    # 隐私预检通过直接放行，不调用 LLM 审查
    assert reviewer.calls == []


def test_invalid_or_private_fetch_is_denied_before_web_reviewer(tmp_path: Path):
    reviewer = WebReviewer()
    manager = PermissionManager(
        str(tmp_path), None, None, DataGuard(), web_safety_client=reviewer
    )
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://example.test:8443/"):
        result = run(manager.authorize(
            "web_fetch",
            policy(),
            {"url": url},
            origin=ToolOrigin("builtin"),
            plan_active=False,
            user_intent="fetch",
            review_model="current-model",
        ))
        assert not result.allowed and result.source == "hard_rule"
    assert reviewer.calls == []


def test_tools_mgr_passes_current_model_and_omits_web_result_preview(tmp_path: Path):
    class Args(BaseModel):
        query: str

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    async def search(query: str) -> str:
        return f"private web content for {query}"

    reviewer = WebReviewer()
    guard = DataGuard()
    permission_mgr = PermissionManager(
        str(tmp_path), None, None, guard, web_safety_client=reviewer
    )
    tools_mgr = ToolsMgr(load_registered=False)
    tools_mgr.register(ToolEntry(
        name="web_search",
        func=search,
        model=Args,
        description="search",
        parameters_schema=Args.model_json_schema(),
        policy=policy(),
        origin=ToolOrigin("builtin"),
    ))
    bus = Bus()
    deps = SimpleNamespace(
        data_guard=guard,
        permission_mgr=permission_mgr,
        hooks_mgr=None,
        event_bus=bus,
        turn_clock=None,
    )
    agent = SimpleNamespace(
        uuid="agent",
        agent_type="main",
        plan_active=True,
        history=[{"role": "user", "content": "research"}],
        llm=SimpleNamespace(model="current-model"),
    )
    result = run(tools_mgr.execute(
        "web_search", {"query": "public docs"}, deps=deps, agent=agent
    ))
    completed = next(event for event in bus.events if isinstance(event, ToolCallCompleted))
    assert "private web content" in result
    # 隐私预检通过直接放行，不调用 LLM 审查
    assert reviewer.calls == []
    assert completed.result_preview.startswith("status=success, length=")
    assert "private web content" not in completed.result_preview
