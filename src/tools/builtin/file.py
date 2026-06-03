from __future__ import annotations
from pathlib import Path, PurePosixPath
from typing import Any, TYPE_CHECKING, Optional

from src.mgr.permission_mgr import (
    ACCEPT_EDITS_MODE, PermissionCheckResult, PermissionContext,
)
from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent


_SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "credentials.json", ".npmrc", ".pypirc"}


def _is_sensitive_path(path: str, workdir: str) -> bool:
    """判断文件路径是否为敏感路径（.git 目录、敏感配置文件、工作目录外路径）。

    Args:
        path: 待检查的文件路径。
        workdir: 工作区根目录路径。

    Returns:
        True 表示路径敏感，需要用户确认。
    """
    if not path:
        return False
    normalized = PurePosixPath(path).as_posix()
    if "/.git/" in normalized or normalized.endswith("/.git"):
        return True
    if PurePosixPath(path).name.lower() in _SENSITIVE_NAMES:
        return True
    try:
        resolved = Path(path).resolve()
        workdir_resolved = Path(workdir).resolve()
        if not str(resolved).startswith(str(workdir_resolved)):
            return True
    except (OSError, ValueError):
        return True
    return False


def check_file_edit_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """文件编辑安全检查：敏感路径强制询问、acceptEdits 模式放行。规则匹配由 check() 统一处理。

    Args:
        tool_input: 工具调用参数。
        ctx: 权限上下文，包含当前模式、工作目录和工具名。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    path = tool_input.get("path") or tool_input.get("file_path") or tool_input.get("source") or ""
    if _is_sensitive_path(path, ctx.workdir):
        return PermissionCheckResult("ask", f"敏感路径需确认：{path}", bypass_immune=True)
    if ctx.mode is ACCEPT_EDITS_MODE:
        return PermissionCheckResult("allow", f"acceptEdits 模式放行文件编辑：{ctx.tool_name}")
    return PermissionCheckResult("passthrough")

class ListDirectory(BaseModel):
    path: Optional[str] = Field(None, description="目录绝对路径，不提供时默认为工作目录。")
    max_depth: Optional[int] = Field(3, description="递归列出子目录的最大深度，默认为3。")

@tool(model=ListDirectory, description="列出目录结构，显示文件和子目录的树形结构。",
      permission=ToolPermission(readonly=True, tips="列出目录：{path}"))
async def list_directory(path: str | None, agent: Agent, max_depth: int = 3) -> str:
    return await agent._file_mgr.list_directory(
        path or str(agent._file_mgr.workdir),
        max_depth=max_depth,
    )

class FindFiles(BaseModel):
    pattern: str = Field(..., description="文件名或目录 glob，只匹配路径，如 '*.py'、'**/config*.yaml'")
    path: Optional[str] = Field(None, description="查找起点目录绝对路径，不提供时默认为工作目录。")

@tool(model=FindFiles, description="查找文件或目录，支持glob。",
      permission=ToolPermission(readonly=True, tips="在 {path} 中查找：{pattern}"))
async def find_files(pattern: str, agent: Agent, path: str | None = None) -> str:
    return await agent._file_mgr.find_files(pattern, path=path or str(agent._file_mgr.workdir))

class SearchFiles(BaseModel):
    query: str = Field(..., description="要查找的普通文本，不区分大小写；不是 glob，也不是正则。")
    path: Optional[str] = Field(None, description="目录或文件的绝对路径。当为目录时，搜索目录中所有文件内容，包括子目录中的文件。不提供时默认为工作目录。不支持 glob。")

@tool(model=SearchFiles, description="搜索文本内容，返回匹配文件、行号和匹配行。",
      permission=ToolPermission(readonly=True, tips="搜索文本：{query}"))
async def search_files(query: str, agent: Agent, path: str | None = None) -> str:
    return await agent._file_mgr.search_files(query, path=path or str(agent._file_mgr.workdir))

class GetFileInfo(BaseModel):
    path: str = Field(..., description="要查询的文件或目录的绝对路径。")

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。",
      permission=ToolPermission(readonly=True, tips="查看文件信息：{path}"))
async def get_file_info(path: str, agent: Agent) -> str:
    return await agent._file_mgr.get_file_info(path)

class ReadFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始；未提供时从文件开头读取。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行；未提供时读取到文件末尾。")

@tool(model=ReadFile, description="读取文件内容并附带行号，可指定行数范围。",
      permission=ToolPermission(readonly=True, specifier_arg="path", tips="读取文件：{path}"))
async def read_file(path: str, agent: Agent,
                    start_line: int | None = None,
                    end_line: int | None = None) -> str:
    return await agent._file_mgr.read_file(path, start_line=start_line, end_line=end_line)

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的绝对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。",
      permission=ToolPermission(specifier_arg="path", tips="创建目录：{path}", check_permissions=check_file_edit_permissions))
async def create_directory(path: str, agent: Agent) -> str:
    return await agent._file_mgr.create_directory(path)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的绝对路径。")
    destination: str = Field(..., description="目标绝对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。",
      permission=ToolPermission(specifier_arg="source", tips="移动或重命名：{source} -> {destination}", check_permissions=check_file_edit_permissions))
async def move_file(source: str, destination: str, agent: Agent) -> str:
    return await agent._file_mgr.move_file(source, destination)

class WriteFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="新建、覆盖写入或追加完整文件内容，支持分块写入大文件。不用精确编辑文件内容",
      permission=ToolPermission(specifier_arg="path", tips="写入文件：{path}", check_permissions=check_file_edit_permissions))
async def write_file(path: str, content: str, agent: Agent,
                     append: bool = False,
                     chunk_index: int | None = None,
                     total_chunks: int | None = None) -> str:
    return await agent._file_mgr.write_file(path, content, append, chunk_index, total_chunks)

class ReplaceInFile(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    old_text: str = Field(..., description="要精确查找的原文本，必须与文件内容完全一致。")
    new_text: str = Field(..., description="替换后的新文本；传空字符串表示删除匹配文本。")
    count: int = Field(1, description="替换次数，0=全部替换。")

@tool(model=ReplaceInFile, description="精确替换文件中的文本。用于已知 old_text 完整内容时的局部编辑；count=0 表示全部替换。",
      permission=ToolPermission(specifier_arg="file_path", tips="替换文件文本：{file_path}", check_permissions=check_file_edit_permissions))
async def replace_in_file(file_path: str, old_text: str, new_text: str,
                          agent: Agent, count: int = 1) -> str:
    return await agent._file_mgr.replace_in_file(file_path, old_text, new_text, count)

class ReplaceFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="要替换的起始行号，从1开始。")
    end_line: int = Field(..., description="要替换的结束行号，包含该行。")
    new_text: str = Field(..., description="用于替换该行范围的新内容。")

@tool(model=ReplaceFileLines, description="按行号范围替换文件内容。适合 read_file 返回行号后进行精确行级编辑。",
      permission=ToolPermission(specifier_arg="file_path", tips="替换文件行：{file_path}，行范围：{start_line}-{end_line}", check_permissions=check_file_edit_permissions))
async def replace_file_lines(file_path: str, start_line: int, end_line: int,
                             new_text: str, agent: Agent) -> str:
    return await agent._file_mgr.replace_file_lines(file_path, start_line, end_line, new_text)

class InsertFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="插入位置，从1开始；内容会插入到该行之前。传总行数+1表示追加到末尾。")
    new_text: str = Field(..., description="要插入的新内容。")

@tool(model=InsertFileLines, description="在指定行之前插入文件内容。传 start_line=总行数+1时 可追加到文件末尾。",
      permission=ToolPermission(specifier_arg="file_path", tips="插入文件行：{file_path}，位置：{start_line}", check_permissions=check_file_edit_permissions))
async def insert_file_lines(file_path: str, start_line: int,
                            new_text: str, agent: Agent) -> str:
    return await agent._file_mgr.insert_file_lines(file_path, start_line, new_text)

class DeleteFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="要删除的起始行号，从1开始。")
    end_line: int = Field(..., description="要删除的结束行号，包含该行。")

@tool(model=DeleteFileLines, description="按行号范围删除文件内容。适合 read_file 返回行号后进行精确删除。",
      permission=ToolPermission(specifier_arg="file_path", tips="删除文件行：{file_path}，行范围：{start_line}-{end_line}", check_permissions=check_file_edit_permissions))
async def delete_file_lines(file_path: str, start_line: int,
                            end_line: int, agent: Agent) -> str:
    return await agent._file_mgr.delete_file_lines(file_path, start_line, end_line)
