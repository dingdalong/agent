"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging

from src.interfaces import CLIInterface
from src.events import EventBus, EventLevel
from src.llm import get_provider
from pathlib import Path

from src.mgr import ConfigManager, MemoryMgr, PermissionManager, ToolsMgr
from src.agent import AgentDeps
from src.app.app import AgentApp

logger = logging.getLogger(__name__)

async def create_app() -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。
    """
    config_mgr = ConfigManager()
    event_bus = EventBus(level=EventLevel.from_str(config_mgr.get_config("events").get("level", "progress")))
    ui = CLIInterface()
    tools_mgr = ToolsMgr()
    workspace = Path.cwd() / "workspace"
    memory_mgr = MemoryMgr(workspace)
    permission_mgr = PermissionManager(
        tools=tools_mgr.list_entries(),
        config_mgr=config_mgr,
    )

    default_llm_cfg = config_mgr.get_config("llm.default")
    llm_provider_name = default_llm_cfg["provider"]
    llm_provider_cfg = config_mgr.get_config(f"llm_provider.{llm_provider_name}")
    LLMProvider = get_provider(llm_provider_name)
    llm = LLMProvider(
        api_key = llm_provider_cfg.get("api_key", ""),
        base_url = llm_provider_cfg["base_url"],
        model = default_llm_cfg["model"],
        reasoning_effort = llm_provider_cfg["reasoning_effort"],
        preserve_thinking = llm_provider_cfg.get("preserve_thinking", False),
        concurrency = default_llm_cfg["concurrency"],
        max_retries = default_llm_cfg["max_retries"],
        context_limit = llm_provider_cfg["context_limit"],
        page_token_rate = config_mgr.get_config("tool.page_token_rate"),
        event_bus = event_bus,
    )
    deps = AgentDeps(
        llm = llm,
        ui = ui,
        event_bus = event_bus,
        tools_mgr = tools_mgr,
        permission_mgr = permission_mgr,
        config_mgr = config_mgr,
        memory_mgr = memory_mgr,
    )
    return AgentApp(
        deps = deps,
    )
