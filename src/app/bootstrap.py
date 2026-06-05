"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging

from src.interfaces import CLIInterface
from src.events import EventBus, EventLevel
from pathlib import Path

from src.mgr import ConfigManager, HooksMgr, LLMMgr, MemoryMgr, PermissionManager, PlanMgr, ToolsMgr
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
    hooks_mgr = HooksMgr(workspace)
    plan_mgr = PlanMgr(workspace)
    permission_mgr = PermissionManager(
        tools=tools_mgr.list_entries(),
        config_mgr=config_mgr,
        workdir=str(workspace),
    )
    llm_mgr = LLMMgr(config_mgr=config_mgr, event_bus=event_bus)
    await llm_mgr.load_models()
    deps = AgentDeps(
        llm_mgr = llm_mgr,
        ui = ui,
        event_bus = event_bus,
        tools_mgr = tools_mgr,
        permission_mgr = permission_mgr,
        config_mgr = config_mgr,
        memory_mgr = memory_mgr,
        hooks_mgr = hooks_mgr,
        plan_mgr = plan_mgr,
        session_context = [],
    )
    return AgentApp(
        deps = deps,
    )
