from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import AgentDeps

class ReadToolResult(BaseModel):
    tool_call_id: str = Field(..., description="被截断工具结果对应的原始 tool_call_id。")
    page: int = Field(2, description="要读取的页码。第 1 页已包含在原始工具返回中；继续读取时通常从 page=2 开始。")

@tool(
    model=ReadToolResult,
    description=(
        "读取分页工具结果的后续页。当工具结果包含 [TOOL_RESULT_TRUNCATED] "
        "或 [TOOL_RESULT_PAGE_INCOMPLETE] 标记时使用；当结果包含 "
        "[TOOL_RESULT_COMPLETE] 标记时不要继续调用。"
    ),
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
            f"如需继续读取，请调用 read_tool_result(tool_call_id=\"{tool_call_id}\", page=2)。"
        )
    try:
        content, current, total = deps.tools_mgr.get_page(tool_call_id, page)
    except KeyError:
        return f"错误：未找到 tool_call_id=\"{tool_call_id}\" 的缓存结果"
    except ValueError as exc:
        return f"错误：{exc}"
    status_marker = "[TOOL_RESULT_PAGE_INCOMPLETE]" if current < total else "[TOOL_RESULT_COMPLETE]"
    header = (
        f"{status_marker}\n"
        f"工具调用 id: \"{tool_call_id}\"\n"
        f"第 {current}/{total} 页。"
        f"本页属于同一工具结果；如缺中间页，先按页码补读；无需输出合并全文。\n\n"
    )
    if current < total:
        footer = (
            f"\n\n本次工具结果尚未读取完整。"
            f"继续调用 read_tool_result(tool_call_id=\"{tool_call_id}\", page={current + 1}) 读取下一页。"
        )
    else:
        footer = "\n\n本次工具结果已全部读取完成。不要再为这个 tool_call_id 继续读取后续页。"
    return header + content + footer
