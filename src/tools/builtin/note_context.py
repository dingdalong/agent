"""共享上下文记录工具 — 把关键发现显式记入跨 agent 账本。

与自动记账的分工：`SubAgentMgr.task_delegator` 已经自动把每个子智能体的返回报告
记进账本，那是主力、零纪律成本。本工具覆盖自动记账抓不到的部分——主 agent 与用户
对话中确认的决策与约束，以及子智能体在长任务中途得出的、不适合等到最终报告才说的
阶段性结论。

策略取 `AccessKind.INTERNAL + plan_safe=True`，与 `save_memory` / `task_create`
同构：落盘由 ContextMgr 内部完成，不经 `write_file`。这一点是刻意的——`.agent`
被 PathResolver 归为 PROTECTED，而 plan 模式下 `_authorize_plan()` 只放行
`PathClass.PLAN`，改用 write_file 落盘会让本工具在 plan 模式下必然被拒。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.tools.policy import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool


class NoteContext(BaseModel):
    topic: str = Field(..., description="一句话标题，说明这条记录讲的是什么。")
    content: str = Field(
        ...,
        description="正文。写已核实的结论与依据，不要写猜测；结论要能让别人无需重新探索即可采信。",
    )
    refs: list[str] | None = Field(
        default=None,
        description="定位信息，形如 src/mgr/foo.py:87 的 file:line 列表，便于他人核对。",
    )


@tool(
    model=NoteContext,
    description=(
        "把关键发现、已核实的结论或已确认的决策记入共享上下文，供本会话其他子任务复用，"
        "避免他们重新探索同一处代码。适合记：与用户确认的决策与约束、跨任务通用的事实、"
        "阶段性结论。不要记：猜测、可从当前对话直接看到的内容、临时状态。"
    ),
    policy=ToolPolicy(
        AccessKind.INTERNAL,
        DataFlow.LOCAL,
        plan_safe=True,
        detail_template="记录 {topic}",
    ),
    subagent=True,
    feature="subagent",
    counts_as_work=False,
)
async def note_context(
    topic: str,
    content: str,
    deps: Any,
    agent: Any,
    refs: list[str] | None = None,
) -> str:
    """把一条发现记入共享上下文账本。

    Args:
        topic: 条目标题。
        content: 正文。
        deps: 依赖容器（自动注入）。
        agent: 当前 Agent 实例（自动注入），用于标记记录方。
        refs: file:line 定位列表（可选）。

    Returns:
        记录结果说明。
    """
    context_mgr = getattr(deps, "context_mgr", None) if deps is not None else None
    if context_mgr is None:
        return "错误：context_mgr 未配置，无法使用共享上下文工具。"

    entry = await context_mgr.record(
        kind="note",
        topic=topic,
        content=content,
        author=getattr(agent, "agent_type", "") or "unknown",
        refs=refs or (),
    )
    if entry is None:
        return "未记录：共享上下文已禁用，或正文过短没有记录价值。"
    return f"已记入共享上下文：[{entry.id}] {entry.topic}"
