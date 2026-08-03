"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging
from pathlib import Path

from prompt_toolkit import PromptSession

from src.interfaces import AgentViewStore, InlineInterface, OutputRouter, TurnClock
from src.events import EventBus, EventLevel
from src.mgr import ConfigManager, HooksMgr, LLMMgr, McpMgr, MemoryMgr, PermissionManager, PlanMgr, PluginMgr, RoleMgr, SessionMgr, ToolsMgr, resolve_features
from src.mgr.data_guard import DataGuard, register_runtime_secrets
from src.mgr.permission_mgr import LLMJudgeClient
from src.mgr.project_trust import ProjectTrustGate
from src.mgr.paths import global_data_dir, workdir as resolve_workdir
from src.agent import AgentDeps
from src.agent.states import SLASH_COMMANDS
from src.app.app import AgentApp

logger = logging.getLogger(__name__)


async def _confirm_project_trust(
    prompt: str,
    session: PromptSession[str] | None = None,
) -> bool:
    """使用独立 prompt-toolkit 会话读取启动阶段的项目信任确认。"""
    prompt_session = session or PromptSession()
    answer = await prompt_session.prompt_async(f"{prompt}[y/N] ", handle_sigint=True)
    return answer.strip().lower() in {"y", "yes"}


async def create_app(
    workdir_override: str | None = None,
) -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    Args:
        workdir_override: 命令行传入的工作目录覆盖值，None 时使用 cwd。

    Returns:
        已完成依赖装配、尚未进入 REPL 的 AgentApp。
    """
    global_dir = global_data_dir()
    global_dir.mkdir(parents=True, exist_ok=True)
    work_dir = resolve_workdir(workdir_override)

    trust_gate = ProjectTrustGate(workdir=work_dir, global_dir=global_dir)
    project_trusted = await trust_gate.ensure_trusted(_confirm_project_trust)
    config_mgr = ConfigManager(
        global_dir=global_dir,
        workdir=work_dir,
        project_trusted=project_trusted,
    )
    data_guard = DataGuard()
    register_runtime_secrets(data_guard, config_mgr, global_dir, work_dir, project_trusted)
    role_mgr = RoleMgr(config_mgr=config_mgr, workdir=work_dir, global_dir=global_dir)
    event_bus = EventBus(level=EventLevel.from_str(config_mgr.get_config("events").get("level", "progress")))
    agent_view_store = AgentViewStore()
    turn_clock = TurnClock()  # 工具执行层与 UI 交互层共享，用于耗时剔除纯人工等待时段
    ui = InlineInterface(
        agent_view_store=agent_view_store,
        slash_commands=SLASH_COMMANDS,
        turn_clock=turn_clock,
    )
    output_router = OutputRouter(
        ui=ui,
        store=agent_view_store,
        passthrough=not ui.is_tty,
    )
    tools_mgr = ToolsMgr()
    # 按激活角色的 feature 集门控 deps 层可插拔 Manager；未启用则注入 None，其工具从 schema 排除。
    feats = resolve_features(role_mgr.manifest.features if role_mgr.manifest else None)
    memory_mgr = MemoryMgr(work_dir, data_guard=data_guard) if "memory" in feats else None
    plugin_mgr = PluginMgr(
        workdir=work_dir,
        global_dir=global_dir,
        role_mgr=role_mgr,
        project_trusted=project_trusted,
    )
    hooks_mgr = HooksMgr(
        workdir=work_dir,
        global_dir=global_dir,
        plugin_mgr=plugin_mgr,
        data_guard=data_guard,
        base_environment=config_mgr.environment,
        project_trusted=project_trusted,
    )
    plan_mgr = PlanMgr(work_dir) if "plan" in feats else None
    mcp_mgr = McpMgr(
        config_mgr=config_mgr,
        tools_mgr=tools_mgr,
        role_mgr=role_mgr,
        workdir=work_dir,
        data_guard=data_guard,
        project_trusted=project_trusted,
    )
    await mcp_mgr.start()
    session_mgr = SessionMgr(global_dir=global_dir, workdir=work_dir, data_guard=data_guard)
    llm_mgr = LLMMgr(config_mgr=config_mgr, event_bus=event_bus)
    await llm_mgr.load_models()
    # 启动前置校验：默认模型不可用时抛 ModelUnavailableError，由 main.cli 捕获后
    # 清晰退出（提示而非深层堆栈）。须在 UI 启动前完成。
    llm_mgr.ensure_default_available()

    async def confirm_once(tool_name: str, detail: str) -> bool:
        if not ui.is_tty:
            return False
        answer = await event_bus.request_permission(tool_name=tool_name, detail=detail)
        return answer.strip().lower() in {"y", "yes"}

    permission_mgr = PermissionManager(
        workdir=str(work_dir),
        judge_client=LLMJudgeClient(llm_mgr, data_guard),
        confirm=confirm_once,
        data_guard=data_guard,
    )
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
        mcp_mgr=mcp_mgr,
        role_mgr=role_mgr,
        turn_clock=turn_clock,
        data_guard=data_guard,
        trust_gate=trust_gate,
        session_context=[],
        workdir=work_dir,
        global_dir=global_dir,
    )
    return AgentApp(
        deps=deps,
        agent_view_store=agent_view_store,
        output_router=output_router,
    )
