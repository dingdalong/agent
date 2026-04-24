from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import AgentDeps

class ReadToolResult(BaseModel):
    tool_use_id: str = Field(..., description="被截断结果的 tool_use_id。")
    page: int = Field(1, description="要读取的页码，从 1 开始。")

@tool(
    model=ReadToolResult,
    description="读取被截断的工具结果的指定页。当工具返回提示'已截断'时使用此工具获取后续内容。",
    raw_output=True,
)
async def read_tool_result(tool_use_id: str, deps: AgentDeps, page: int = 1) -> str:
    try:
        content, current, total = deps.tools_mgr.get_page(tool_use_id, page)
    except KeyError:
        return f"错误：未找到 tool_use_id=\"{tool_use_id}\" 的缓存结果"
    header = f"第 {current}/{total} 页:\n"
    if current < total:
        footer = f"\n\n(还有 {total - current} 页，传入 page={current + 1} 继续读取)"
    else:
        footer = "\n\n(已到最后一页)"
    return header + content + footer
