"""任务管理工具 — 创建、更新、列出、查看任务。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field

from src.tools.decorator import ToolPermission, tool

if TYPE_CHECKING:
    from src.agent import Agent


# ── task_create ────────────────────────────────────────────────────

class TaskCreateModel(BaseModel):
    """创建新任务。"""
    subject: str = Field(..., description='简短的任务标题，祈使句形式（如"修复认证 bug"）')
    description: str = Field(..., description="完整的任务需求描述")
    active_form: str | None = Field(default=None, description="进行时描述，用于 spinner 显示")
    metadata: Dict[str, Any] | None = Field(default=None, description="任意键值对")


@tool(
    model=TaskCreateModel,
    description="创建新任务（状态为 pending）。",
    permission=ToolPermission(kind="readonly"),
    subagent=False,
)
async def task_create(
    subject: str,
    description: str,
    agent: Agent,
    active_form: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """创建任务并返回 ID 和标题。

    Args:
        subject: 任务标题。
        description: 任务需求描述。
        agent: 当前 Agent 实例（自动注入）。
        active_form: 进行时描述（可选）。
        metadata: 任意键值对（可选）。

    Returns:
        JSON 字符串，包含新任务的 id 和 subject。
    """
    try:
        result = agent._task_mgr.create(subject, description, active_form, metadata)
        return json.dumps(result, ensure_ascii=False)
    except ValueError as e:
        return str(e)


# ── task_update ────────────────────────────────────────────────────

class TaskUpdateModel(BaseModel):
    """更新现有任务。"""
    task_id: str = Field(..., description="任务 ID")
    subject: str | None = Field(default=None, description="新标题")
    description: str | None = Field(default=None, description="新描述")
    active_form: str | None = Field(default=None, description="新进行时描述")
    status: Literal["pending", "in_progress", "completed", "deleted"] | None = Field(
        default=None, description="新状态。开始执行前标记 in_progress；完全完成后标记 completed；deleted 删除任务并清理依赖")
    owner: str | None = Field(default=None, description="认领任务的智能体标识符")
    add_blocks: List[str] | None = Field(default=None, description="追加到 blocks 的任务 ID")
    add_blocked_by: List[str] | None = Field(default=None, description="追加到 blocked_by 的任务 ID")
    metadata: Dict[str, Any] | None = Field(default=None, description="合并更新的键值对，值为 null 删除键")


@tool(
    model=TaskUpdateModel,
    description="更新任务字段。",
    permission=ToolPermission(kind="readonly"),
    subagent=False,
)
async def task_update(
    task_id: str,
    agent: Agent,
    subject: str | None = None,
    description: str | None = None,
    active_form: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    add_blocks: list[str] | None = None,
    add_blocked_by: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """更新任务并返回结果。

    Args:
        task_id: 目标任务 ID。
        agent: 当前 Agent 实例（自动注入）。
        subject: 新标题（可选）。
        description: 新描述（可选）。
        active_form: 新进行时描述（可选）。
        status: 新状态（可选），"deleted" 触发删除。
        owner: 认领任务的智能体标识符（可选）。
        add_blocks: 追加到 blocks 的任务 ID（可选）。
        add_blocked_by: 追加到 blocked_by 的任务 ID（可选）。
        metadata: 合并更新的键值对（可选）。

    Returns:
        JSON 字符串，包含 success、task_id、updated_fields。
    """
    try:
        result = agent._task_mgr.update(
            task_id,
            subject=subject,
            description=description,
            active_form=active_form,
            status=status,
            owner=owner,
            add_blocks=add_blocks,
            add_blocked_by=add_blocked_by,
            metadata=metadata,
        )
        return json.dumps(result, ensure_ascii=False)
    except ValueError as e:
        return str(e)


# ── task_list ──────────────────────────────────────────────────────

class TaskListModel(BaseModel):
    """列出所有任务。"""
    pass


@tool(
    model=TaskListModel,
    description="列出所有任务的摘要，包含 ID、标题、状态、未完成的依赖。",
    permission=ToolPermission(kind="readonly"),
    subagent=False,
)
async def task_list(agent: Agent) -> str:
    """返回任务列表 JSON。

    Args:
        agent: 当前 Agent 实例（自动注入）。

    Returns:
        JSON 字符串，包含 tasks 数组。
    """
    result = agent._task_mgr.list_tasks()
    return json.dumps(result, ensure_ascii=False)


# ── task_get ───────────────────────────────────────────────────────

class TaskGetModel(BaseModel):
    """查看任务详情。"""
    task_id: str = Field(..., description="任务 ID")


@tool(
    model=TaskGetModel,
    description="查看任务的完整详情，包含描述、依赖关系。",
    permission=ToolPermission(kind="readonly"),
    subagent=False,
)
async def task_get(task_id: str, agent: Agent) -> str:
    """返回任务完整详情 JSON。

    Args:
        task_id: 目标任务 ID。
        agent: 当前 Agent 实例（自动注入）。

    Returns:
        JSON 字符串，包含任务完整字段。
    """
    try:
        result = agent._task_mgr.get_task(task_id)
        return json.dumps(result, ensure_ascii=False)
    except ValueError as e:
        return str(e)
