"""斜杠命令 /plan — 进入计划模式。"""

from __future__ import annotations

import time

from src.commands import command
from src.commands.context import CommandContext
from src.events.types import PlanStateChanged


@command("进入计划模式", feature="plan")
async def plan(ctx: CommandContext, args: list[str]) -> None:
    """切换前台 Agent 到计划模式并广播状态事件。"""
    agent = ctx.agent
    if agent is None:
        return
    if not agent.set_plan_active(True):
        await ctx.deps.event_bus.request_output("已在计划模式中。\n")
        return
    await ctx.deps.event_bus.emit(PlanStateChanged(
        timestamp=time.time(), source=agent.agent_type, active=True,
    ))
    await ctx.deps.event_bus.request_output("已进入计划模式。\n")
