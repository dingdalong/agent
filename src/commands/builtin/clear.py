"""斜杠命令 /clear — 重置会话并更换前台 Agent。

app 层命令：需主循环上下文，经 AgentApp.reset_session 重置全部共享状态。
"""

from __future__ import annotations

from src.commands import command
from src.commands.context import CommandContext, CommandResult


@command("清空会话", layer="app")
async def clear(ctx: CommandContext, args: list[str]) -> CommandResult:
    """经 app 门面重置会话，返回新前台 Agent 供主循环替换。"""
    new_agent = await ctx.app.reset_session(source="clear")
    await ctx.deps.event_bus.request_output("上下文已清理，所有组件已重载。\n")
    return CommandResult(new_agent=new_agent)
