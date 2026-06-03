from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import AgentDeps

class ReadToolResult(BaseModel):
    tool_call_id: str = Field(..., description="被截断工具结果对应的原始 tool_call_id。")
    page: int = Field(2, description="页码，从第 2 页继续读取。")

@tool(
    model=ReadToolResult,
    description="读取分页工具结果的后续页。",
    permission=ToolPermission(readonly=True),
    raw_output=True,
)
async def read_tool_result(
    deps: AgentDeps,
    tool_call_id: str,
    page: int = 2,
) -> str:
    if page == 1:
        return (
            f"第 1 页已包含在原始工具返回中。"
            f"如需继续读取，传入 tool_call_id={tool_call_id}, page=2。"
        )
    try:
        return deps.tools_mgr.get_page(tool_call_id, page)
    except KeyError:
        return f"错误：未找到 tool_call_id=\"{tool_call_id}\" 的缓存结果"
    except ValueError as exc:
        return f"错误：{exc}"
