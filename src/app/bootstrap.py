"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import time
from collections.abc import Callable
from pathlib import Path

from src.interfaces import AgentViewStore, OutputRouter, TextualInterface, TurnClock
from src.interfaces.tui.plain import LineReader, read_console_line
from src.events import EventBus, EventLevel
from src.events.types import TaskStateChanged
from src.mgr import ConfigManager, HooksMgr, LLMMgr, McpMgr, MemoryMgr, PermissionManager, PlanMgr, PluginMgr, RoleMgr, SessionMgr, ToolsMgr, WebAccessMgr, resolve_features
from src.mgr.data_guard import DataGuard, register_runtime_secrets
from src.mgr.session_state import SessionState
from src.mgr.permission_mgr import LLMJudgeClient
from src.mgr.web_safety_mgr import LLMWebSafetyClient
from src.mgr.project_trust import ProjectTrustGate
from src.mgr.paths import global_data_dir, workdir as resolve_workdir
from src.agent import AgentDeps
from src.commands import CommandMgr
from src.app.app import AgentApp

logger = logging.getLogger(__name__)


def _make_task_notifier(bus: EventBus) -> Callable[[list[dict]], None]:
    """创建线程安全的任务变更回调，将 TaskStateChanged 事件投递到 EventBus。"""
    def notify(tasks_summary: list[dict]) -> None:
        event = TaskStateChanged(
            timestamp=time.time(),
            source="task_mgr",
            tasks=tasks_summary,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.run_coroutine_threadsafe(bus.emit(event), loop)
    return notify


async def _confirm_project_trust(
    prompt: str,
    reader: LineReader = read_console_line,
) -> bool:
    """使用独立纯文本输入读取启动阶段的项目信任确认。"""
    answer = await reader(f"{prompt}[y/N] ")
    return answer.strip().lower() in {"y", "yes"}


async def create_app(
    workdir_override: str | None = None,
    *,
    copy_on_select: bool | None = None,
) -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    Args:
        workdir_override: 命令行传入的工作目录覆盖值，None 时使用 cwd。
        copy_on_select: 是否在鼠标选中后立即复制；None 使用平台默认值。

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

    # 配置项目级日志：写入 {workdir}/.agent/logs/agent.log
    log_dir = work_dir / ".agent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)
    log_level_str = config_mgr.get_config("logging").get("level", "info")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    os.chmod(log_dir / "agent.log", 0o600)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.root.addHandler(file_handler)
    logging.root.setLevel(log_level)

    data_guard = DataGuard()
    register_runtime_secrets(data_guard, config_mgr, global_dir, work_dir, project_trusted)
    role_mgr = RoleMgr(config_mgr=config_mgr, workdir=work_dir, global_dir=global_dir)
    # 按激活角色的 feature 集门控 deps 层可插拔 Manager；未启用则注入 None，其工具从 schema 排除。
    feats = resolve_features(role_mgr.manifest.features if role_mgr.manifest else None)
    command_mgr = CommandMgr(workdir=work_dir, global_dir=global_dir, project_trusted=project_trusted)
    event_bus = EventBus(level=EventLevel.from_str(config_mgr.get_config("events").get("level", "progress")))
    agent_view_store = AgentViewStore()
    session_state = SessionState()
    turn_clock = TurnClock()  # 工具执行层与 UI 交互层共享，用于耗时剔除纯人工等待时段
    ui = TextualInterface(
        agent_view_store=agent_view_store,
        slash_commands=command_mgr.completion_items(feats),
        turn_clock=turn_clock,
        copy_on_select=copy_on_select,
        diagnostic_dir=global_dir / "logs",
        data_guard=data_guard,
    )
    output_router = OutputRouter(
        ui=ui,
        store=agent_view_store,
        passthrough=not ui.is_tty,
        session_state=session_state,
    )
    tools_mgr = ToolsMgr()
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

    async def confirm_once(tool_name: str, detail: str, reason: str = "") -> bool:
        if not ui.is_tty:
            return False
        answer = await event_bus.request_permission(tool_name=tool_name, detail=detail, reason=reason)
        return answer.strip().lower() in {"y", "yes"}

    permission_mgr = PermissionManager(
        workdir=str(work_dir),
        judge_client=LLMJudgeClient(llm_mgr, data_guard),
        confirm=confirm_once,
        data_guard=data_guard,
        web_safety_client=LLMWebSafetyClient(llm_mgr, data_guard),
    )
    web_access_mgr = WebAccessMgr(llm_mgr)
    deps = AgentDeps(
        llm_mgr=llm_mgr,
        ui=ui,
        event_bus=event_bus,
        tools_mgr=tools_mgr,
        permission_mgr=permission_mgr,
        web_access_mgr=web_access_mgr,
        config_mgr=config_mgr,
        memory_mgr=memory_mgr,
        hooks_mgr=hooks_mgr,
        plan_mgr=plan_mgr,
        plugin_mgr=plugin_mgr,
        session_mgr=session_mgr,
        mcp_mgr=mcp_mgr,
        role_mgr=role_mgr,
        turn_clock=turn_clock,
        command_mgr=command_mgr,
        data_guard=data_guard,
        trust_gate=trust_gate,
        task_change_notifier=_make_task_notifier(event_bus),
        session_context=[],
        session_state=session_state,
        workdir=work_dir,
        global_dir=global_dir,
    )
    return AgentApp(
        deps=deps,
        agent_view_store=agent_view_store,
        output_router=output_router,
    )
