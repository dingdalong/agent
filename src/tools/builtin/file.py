from __future__ import annotations
from typing import TYPE_CHECKING, Optional

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class ReadFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    limit: Optional[int] = Field(None, description="读取文件行数限制。")

@tool(model=ReadFile, description="读取文件内容。")
async def read_file(path: str, tool_use_id: str, agent: Agent, limit: int | None = None) -> str:
    return await agent._file.read_file(path, tool_use_id, limit)

class WriteFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    content: str = Field(..., description="要写入文件的内容")

@tool(model=WriteFile, description="将内容写到文件")
async def write_file(path: str, content: str, agent: Agent) -> str:
    return await agent._file.write_file(path, content)

class EditFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    old_text: str = Field(..., description="旧文本")
    new_text: str = Field(..., description="新文本")

@tool(model=EditFile, description="精确替换文件中的文本。")
async def edit_file(path: str, old_text: str, new_text: str, agent: Agent) -> str:
    return await agent._file.edit_file(path, old_text, new_text)

