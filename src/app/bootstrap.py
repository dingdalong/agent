"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging
from pathlib import Path

from src.interfaces import InlineInterface, OutputRouter
from src.events import EventBus, EventLevel
from src.mgr import ConfigManager, HooksMgr, LLMMgr, MemoryMgr, PermissionManager, PlanMgr, PluginMgr, SessionMgr, ToolsMgr
from src.mgr.paths import global_data_dir, workdir as resolve_workdir
from src.agent import AgentDeps
from src.app.app import AgentApp

logger = logging.getLogger(__name__)


async def create_app(
    workdir_override: str | None = None,
) -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    Args:
        workdir_override: 命令行传入的工作目录覆盖值，None 时使用 cwd。
    """
    global_dir = global_data_dir()
    global_dir.mkdir(parents=True, exist_ok=True)
    work_dir = resolve_workdir(workdir_override)

    config_mgr = ConfigManager(global_dir=global_dir, workdir=work_dir)
    event_bus = EventBus(level=EventLevel.from_str(config_mgr.get_config("events").get("level", "progress")))
    ui = InlineInterface()
    output_router = OutputRouter(ui=ui, passthrough=not ui.is_tty)
    ui.set_agent_source(output_router.agent_rows, output_router.render_transcript)
    tools_mgr = ToolsMgr()
    memory_mgr = MemoryMgr(work_dir)
    plugin_mgr = PluginMgr(workdir=work_dir, global_dir=global_dir)
    hooks_mgr = HooksMgr(workdir=work_dir, global_dir=global_dir, plugin_mgr=plugin_mgr)
    plan_mgr = PlanMgr(work_dir)
    permission_mgr = PermissionManager(
        tools=tools_mgr.list_entries(),
        config_mgr=config_mgr,
        workdir=str(work_dir),
        trusted_dirs=(str(global_dir),),
    )
    session_mgr = SessionMgr(global_dir=global_dir, workdir=work_dir)
    llm_mgr = LLMMgr(config_mgr=config_mgr, event_bus=event_bus)
    await llm_mgr.load_models()
    # 启动前置校验：默认模型不可用时抛 ModelUnavailableError，由 main.cli 捕获后
    # 清晰退出（提示而非深层堆栈）。须在 UI 启动前完成。
    llm_mgr.ensure_default_available()
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
        plugin_mgr=plugin_mgr,
        session_mgr=session_mgr,
        output_router=output_router,
        session_context=[],
        workdir=work_dir,
        global_dir=global_dir,
    )
    return AgentApp(deps=deps)
