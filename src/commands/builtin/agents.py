"""斜杠命令 /agents — 浏览本会话子 agent。

app 层命令：弹出可方向键选择的子 agent 列表，选中后以只读面板回看其完整原始消息记录。
循环：每轮重取列表（运行中的子 agent 可能已完成）→ request_choice 选择 → 选中则
request_transcript_view 打开面板 → Esc 返回列表 → 直至列表 Esc 取消退出。非 TTY 环境
无富交互面板，退回打印纯文本摘要。
"""

from __future__ import annotations

import asyncio

from src.commands import command
from src.commands.context import CommandContext
from src.events import NoEventSubscribers
from src.interfaces.status_presenter import present_agent


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@command("查看本会话子 agent", layer="app")
async def agents(ctx: CommandContext, args: list[str]) -> None:
    """弹出子 agent 列表并支持只读回看；非 TTY 打印纯文本摘要。"""
    deps = ctx.deps
    agent_view_store = ctx.app.agent_view_store

    if not deps.ui.is_tty:
        snapshots = agent_view_store.subagent_snapshots()
        if not snapshots:
            summary = "本会话尚未启动任何子 agent。"
        else:
            lines = [f"本会话子 agent（{len(snapshots)}）:"]
            lines.extend(present_agent(snapshot).plain for snapshot in snapshots)
            summary = "\n".join(lines)
        await deps.event_bus.request_output(summary + "\n")
        return

    while True:
        snapshots = agent_view_store.subagent_snapshots()
        choices = [
            (snapshot.uuid, present_agent(snapshot).plain)
            for snapshot in snapshots
        ]
        if not choices:
            await deps.event_bus.request_output("暂无子 agent 记录。\n")
            return
        try:
            picked = await deps.event_bus.request_choice(
                "\n子 agent 历史（选择查看完整消息记录）", choices, 0
            )
        except asyncio.CancelledError:
            if _current_task_is_cancelling():
                raise
            return
        except NoEventSubscribers:
            return
        if not picked:  # Esc 取消，退出浏览
            return
        try:
            await deps.event_bus.request_transcript_view(picked)
        except asyncio.CancelledError:
            if _current_task_is_cancelling():
                raise
            return
        except NoEventSubscribers:
            return
