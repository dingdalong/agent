from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.mgr.memory_mgr import MemoryType
from src.tools.policy import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool


class SaveMemory(BaseModel):
    title: str = Field(..., description="标题。")
    description: str = Field(..., description="一句话说明这条记忆的用途。")
    type: MemoryType = Field(..., description="记忆类型。")
    body: str = Field(..., description="记忆正文，使用 Markdown。")


class ReadMemory(BaseModel):
    title: str = Field(..., description="要读取的记忆标题。")


def _memory_mgr(deps: Any) -> Any:
    memory_mgr = getattr(deps, "memory_mgr", None) if deps is not None else None
    if memory_mgr is None:
        return "错误：memory_mgr 未配置，无法使用项目记忆工具。"
    return memory_mgr


@tool(
    model=SaveMemory,
    description=(
        "保存一条新的项目记忆。只保存长期有用的偏好、反馈、项目约定和参考；"
        "不要保存秘密、临时状态或可从代码读取的信息。同标题记忆会被全量覆盖。"
        "保存前必须检查已知记忆；如果语义相近，复用已有标题，合并新旧内容后覆盖，"
        "不要创建近似重复记忆。"
    ),
    policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True),
    feature="memory",
)
async def save_memory(
    title: str,
    description: str,
    type: str,
    body: str,
    deps: Any,
) -> str:
    memory_mgr = _memory_mgr(deps)
    if isinstance(memory_mgr, str):
        return memory_mgr
    result = memory_mgr.save(
        title=title,
        description=description,
        type=type,
        body=body,
    )
    if result.startswith("错误："):
        return result
    return f"已保存项目记忆：{result}"

@tool(
    model=ReadMemory,
    description="读取一条项目记忆的完整内容。",
    policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True),
    feature="memory",
)
async def read_memory(title: str, deps: Any) -> str:
    memory_mgr = _memory_mgr(deps)
    if isinstance(memory_mgr, str):
        return memory_mgr
    return memory_mgr.read(title)
