"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging
from pathlib import Path

from src.interfaces import CLIInterface
from src.events import EventBus, EventLevel
from src.mgr import ConfigManager, HooksMgr, LLMMgr, MemoryMgr, PermissionManager, PlanMgr, ToolsMgr
from src.mgr.paths import agent_home, workdir as resolve_workdir, ensure_global_config
from src.agent import AgentDeps
from src.app.app import AgentApp

logger = logging.getLogger(__name__)


async def create_app(
    workdir_override: str | None = None,
    config_home_override: str | None = None,
) -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    Args:
        workdir_override: 命令行传入的工作目录覆盖值，None 时使用 cwd。
        config_home_override: 命令行传入的全局配置目录覆盖值，None 时使用 ~/.agent/。
    """
    config_home = Path(config_home_override) if config_home_override else agent_home()
    ensure_global_config(config_home)
    work_dir = resolve_workdir(workdir_override)

    config_mgr = ConfigManager(config_home=config_home, workdir=work_dir)
    event_bus = EventBus(level=EventLevel.from_str(config_mgr.get_config("events").get("level", "progress")))
    ui = CLIInterface()
    tools_mgr = ToolsMgr()
    memory_mgr = MemoryMgr(work_dir)
    hooks_mgr = HooksMgr(work_dir)
    plan_mgr = PlanMgr(work_dir)
    permission_mgr = PermissionManager(
        tools=tools_mgr.list_entries(),
        config_mgr=config_mgr,
        workdir=str(work_dir),
    )
    llm_mgr = LLMMgr(config_mgr=config_mgr, event_bus=event_bus)
    await llm_mgr.load_models()
    deps = AgentDeps(
        llm_mgr=llm_mgr,
        ui=ui,
        event_bus=event_bus,
        tools_mgr=tools_mgr,
        permission_mgr=permission_mgr,
        config_mgr=config_mgr,
        memory_mgr=memory_mgr,
        hooks_mgr=hooks_mgr,
        plan_mgr=plan_mgr,
        session_context=[],
        workdir=work_dir,
    )
    return AgentApp(deps=deps)
