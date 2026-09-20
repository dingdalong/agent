from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from src.tools import AccessKind, DataFlow, ToolOrigin, ToolPolicy
from src.events.types import ToolCallCompleted
from src.common.data_guard import DataGuard
from src.mgr.permission_mgr import PermissionManager
from src.mgr.permission_mgr import ToolAuthorizationRequest, ToolCallerContext
from src.mgr.tools_mgr import ToolsMgr
from src.common.features import ALL_FEATURES
from src.mode import RunMode
from src.tools.decorator import ToolEntry
from src.tools.policy import DEFAULT_AVAILABILITY


def run(coro):
    return asyncio.run(coro)


def policy() -> ToolPolicy:
    return ToolPolicy(AccessKind.EXTERNAL_READ, DataFlow.EXTERNAL)


def authorize(
    manager: PermissionManager,
    tool_name: str,
    arguments: dict,
    *,
    mode: RunMode,
    user_intent: str,
):
    return manager.authorize(ToolAuthorizationRequest(
        tool_name=tool_name,
        policy=policy(),
        availability=DEFAULT_AVAILABILITY,
        arguments=arguments,
        origin=ToolOrigin("builtin"),
        caller=ToolCallerContext(
            mode=mode,
            agent_type="main",
            is_subagent=False,
            features=frozenset(ALL_FEATURES),
            declared_tools=None,
            unavailable_tools=(),
        ),
        user_intent=user_intent,
    ))


def test_safe_web_fetch_is_allowed_by_privacy_precheck(tmp_path: Path):
    """隐私预检通过的安全 URL 应直接放行。"""
    manager = PermissionManager(str(tmp_path), None, None, DataGuard())
    result = run(authorize(
        manager,
        "web_fetch",
        {"url": "https://example.test/doc?lang=zh"},
        mode=RunMode.PLAN,
        user_intent="读取这份公开文档",
    ))
    assert result.allowed and result.source == "web_privacy"


def test_personal_search_asks_once(tmp_path: Path):
    confirmations = []

    async def confirm(tool_name: str, detail: str, reason: str = "") -> bool:
        confirmations.append((tool_name, detail))
        return True

    manager = PermissionManager(str(tmp_path), None, confirm, DataGuard())
    result = run(authorize(
        manager,
        "web_search",
        {"query": "alice@example.com recent profile", "max_results": 5},
        mode=RunMode.PLAN,
        user_intent="search",
    ))
    assert result.allowed and result.source == "user"
    assert confirmations == [("web_search", "搜索网页：<query:length=32>")]


def test_secret_is_denied_by_privacy_precheck(tmp_path: Path):
    secret = "sk-this-is-a-secret-value"
    manager = PermissionManager(str(tmp_path), None, None, DataGuard())
    result = run(authorize(
        manager,
        "web_search",
        {"query": secret, "max_results": 5},
        mode=RunMode.EXECUTE,
        user_intent="search",
    ))
    assert not result.allowed and result.source == "hard_rule"
    assert secret not in result.reason


def test_normal_long_search_does_not_trigger_false_private_identifier(tmp_path: Path):
    manager = PermissionManager(str(tmp_path), None, None, DataGuard())
    result = run(authorize(
        manager,
        "web_search",
        {"query": "OpenAI Responses API web search documentation", "max_results": 5},
        mode=RunMode.EXECUTE,
        user_intent="research",
    ))
    assert result.allowed


def test_invalid_or_private_fetch_is_denied_by_privacy_precheck(tmp_path: Path):
    manager = PermissionManager(str(tmp_path), None, None, DataGuard())
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://example.test:8443/"):
        result = run(authorize(
            manager,
            "web_fetch",
            {"url": url},
            mode=RunMode.EXECUTE,
            user_intent="fetch",
        ))
        assert not result.allowed and result.source == "hard_rule"


def test_tools_mgr_omits_web_result_preview(tmp_path: Path):
    class Args(BaseModel):
        query: str

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    async def search(query: str) -> str:
        return f"private web content for {query}"

    guard = DataGuard()
    permission_mgr = PermissionManager(str(tmp_path), None, None, guard)
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
        mode=RunMode.PLAN,
        is_subagent=False,
        features=set(ALL_FEATURES),
        tools=None,
        history=[{"role": "user", "content": "research"}],
        llm=SimpleNamespace(model="current-model"),
    )
    result = run(tools_mgr.execute(
        "web_search", {"query": "public docs"}, deps=deps, agent=agent
    ))
    completed = next(event for event in bus.events if isinstance(event, ToolCallCompleted))
    assert "private web content" in result.text
    assert completed.result_preview.startswith("status=success, length=")
    assert "private web content" not in completed.result_preview
