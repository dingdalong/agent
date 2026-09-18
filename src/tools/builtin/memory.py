from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.mgr.memory_mgr import MemoryType
from src.mode import RunMode
from src.tools.policy import AccessKind, DataFlow, ToolAvailability, ToolPolicy
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
        raise ValueError("memory_mgr 未配置，无法使用项目记忆工具。")
    return memory_mgr


@tool(
    model=SaveMemory,
    description="保存或全量覆盖一条项目记忆。",
    policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True),
    availability=ToolAvailability(frozenset({RunMode.EXECUTE}), feature="memory"),
)
def save_memory(
    title: str,
    description: str,
    type: str,
    body: str,
    deps: Any,
) -> str:
    memory_mgr = _memory_mgr(deps)
    result = memory_mgr.save(
        title=title,
        description=description,
        type=type,
        body=body,
    )
    return f"已保存项目记忆：{result}"

@tool(
    model=ReadMemory,
    description="读取一条项目记忆的完整内容。",
    policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True),
    availability=ToolAvailability(feature="memory"),
)
def read_memory(title: str, deps: Any) -> str:
    memory_mgr = _memory_mgr(deps)
    return memory_mgr.read(title)
