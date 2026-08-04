"""斜杠命令 /help — 列出当前可用的斜杠命令。"""

from __future__ import annotations

from src.commands import command
from src.commands.context import CommandContext


@command("查看可用斜杠命令")
async def help(ctx: CommandContext, args: list[str]) -> None:
    """列出所有可见命令的用法与说明，按 usage 对齐。"""
    command_mgr = ctx.deps.command_mgr
    if command_mgr is None:
        await ctx.deps.event_bus.request_output("命令管理器未初始化。\n")
        return
    features = ctx.agent.features if ctx.agent is not None else None
    entries = command_mgr.list_commands(features=features)
    if not entries:
        await ctx.deps.event_bus.request_output("当前没有可用命令。\n")
        return

    width = max(len(entry.usage) for entry in entries)
    lines = ["可用命令："]
    for entry in entries:
        lines.append(f"  {entry.usage.ljust(width)}  {entry.description}")
    await ctx.deps.event_bus.request_output("\n".join(lines) + "\n")
