"""斜杠命令 /resume — 恢复历史会话。

无参时弹出方向键选择菜单，再委托 SessionMgr 解析会话；
状态变更（替换历史、重建任务、恢复 plan）由 Agent.apply_resume 完成。
"""

from __future__ import annotations

import asyncio

from src.commands import command
from src.commands.context import CommandContext
from src.events import NoEventSubscribers


@command("恢复历史会话", usage="/resume [序号 | session_id]")
async def resume(ctx: CommandContext, args: list[str]) -> None:
    """解析目标会话并应用到前台 Agent。"""
    deps = ctx.deps
    agent = ctx.agent
    session_mgr = deps.session_mgr
    if session_mgr is None:
        await deps.event_bus.request_output("会话管理器未初始化。\n")
        return
    if agent is None:
        return

    cmd_args = list(args)

    # 无参：弹出方向键选择菜单让用户挑选历史会话，选中后转为以 session_id 解析
    if not cmd_args:
        sessions = session_mgr.list_resumable(deps.session_id)
        if not sessions:
            await deps.event_bus.request_output("没有可恢复的历史会话。\n")
            return
        options: list[tuple[str, str]] = []
        for s in sessions:
            updated = s.get("updated_at", "?")[:19].replace("T", " ")
            label = f"[{updated}] {s.get('topic') or s.get('workdir', '')}"
            options.append((s["session_id"], label))
        try:
            picked = await deps.event_bus.request_choice("\n最近的历史会话", options, 0)
        except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
            return
        if not picked:  # Esc 取消，静默
            return
        cmd_args = [picked]

    result = session_mgr.resolve_resume(
        cmd_args,
        current_session_id=deps.session_id,
        current_workdir=str(deps.workdir) if deps.workdir else "",
    )

    # 解析失败或列出会话：直接输出文本
    if isinstance(result, str):
        await deps.event_bus.request_output(result)
        return

    summary = await agent.apply_resume(result)
    await deps.event_bus.request_output(summary)
