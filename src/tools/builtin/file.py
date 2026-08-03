from __future__ import annotations
from typing import TYPE_CHECKING, Optional

from src.tools import AccessKind, DataFlow, PathArgument, PathRole, ToolPolicy
from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent


READ_PATH = (PathArgument("path", PathRole.READ),)
WRITE_PATH = (PathArgument("path", PathRole.WRITE),)
WRITE_FILE_PATH = (PathArgument("file_path", PathRole.WRITE),)


class ListDirectory(BaseModel):
    path: Optional[str] = Field(None, description="目录绝对路径，不提供时默认为工作目录。")
    max_depth: Optional[int] = Field(3, description="递归列出子目录的最大深度，默认为3。")

@tool(model=ListDirectory, description="列出目录结构，显示文件和子目录的树形结构。",
      policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, READ_PATH, True, "列出目录：{path}"), feature="file")
def list_directory(path: str | None, agent: Agent, authorization, max_depth: int = 3) -> str:
    return agent._file_mgr.list_directory(
        path or str(agent._file_mgr.workdir),
        authorization,
        max_depth=max_depth,
    )

class Glob(BaseModel):
    pattern: str = Field(..., description="文件名/路径 glob，默认递归匹配，如 '*.py'、'**/config*.yaml'。只匹配路径，不读取内容。")
    path: Optional[str] = Field(None, description="查找起点目录绝对路径，不提供时默认为工作目录。")

@tool(model=Glob, description="用 ripgrep 按 glob 查找文件，遵守 .gitignore、排除隐藏文件，只返回文件（不含目录）。",
      policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, READ_PATH, True, "在 {path} 中查找：{pattern}"), feature="file")
def glob(pattern: str, agent: Agent, authorization, path: str | None = None) -> str:
    return agent._file_mgr.glob(
        pattern, authorization, path=path or str(agent._file_mgr.workdir)
    )

class Grep(BaseModel):
    pattern: str = Field(..., description="ripgrep 正则表达式，默认区分大小写；搜字面符号 . ( [ * 等需转义。不是 glob。")
    path: Optional[str] = Field(None, description="目录或文件的绝对路径。为目录时递归搜索其中所有文件内容。不提供时默认为工作目录。不支持 glob。")

@tool(model=Grep, description="用 ripgrep 正则搜索文件内容，返回匹配的文件、行号和匹配行，遵守 .gitignore。",
      policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, READ_PATH, True, "搜索内容：{pattern}"), feature="file")
def grep(pattern: str, agent: Agent, authorization, path: str | None = None) -> str:
    return agent._file_mgr.grep(
        pattern, authorization, path=path or str(agent._file_mgr.workdir)
    )

class GetFileInfo(BaseModel):
    path: str = Field(..., description="要查询的文件或目录的绝对路径。")

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。",
      policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, READ_PATH, True, "查看文件信息：{path}"), feature="file")
def get_file_info(path: str, agent: Agent, authorization) -> str:
    return agent._file_mgr.get_file_info(path, authorization)

class ReadFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始；未提供时从文件开头读取。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行；未提供时读取到文件末尾。")

@tool(model=ReadFile, description="读取文件内容并附带行号，可指定行数范围。",
      policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, READ_PATH, True, "读取文件：{path}"), feature="file")
def read_file(path: str, agent: Agent, authorization,
              start_line: int | None = None,
              end_line: int | None = None) -> str:
    return agent._file_mgr.read_file(
        path, authorization, start_line=start_line, end_line=end_line
    )

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的绝对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。",
      policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL, WRITE_PATH, False, "创建目录：{path}"), feature="file")
def create_directory(path: str, agent: Agent, authorization) -> str:
    return agent._file_mgr.create_directory(path, authorization)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的绝对路径。")
    destination: str = Field(..., description="目标绝对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。",
      policy=ToolPolicy(
          AccessKind.REVIEW,
          DataFlow.LOCAL,
          (PathArgument("source", PathRole.SOURCE), PathArgument("destination", PathRole.DESTINATION)),
          False,
          "移动或重命名：{source} -> {destination}",
      ), feature="file")
def move_file(source: str, destination: str, agent: Agent, authorization) -> str:
    return agent._file_mgr.move_file(source, destination, authorization)

class WriteFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="新建、覆盖写入或追加完整文件内容，支持分块写入大文件。不用精确编辑文件内容",
      policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL, WRITE_PATH, False, "写入文件：{path}"), feature="file")
def write_file(path: str, content: str, agent: Agent, authorization,
               append: bool = False,
               chunk_index: int | None = None,
               total_chunks: int | None = None) -> str:
    return agent._file_mgr.write_file(
        path, content, authorization, append, chunk_index, total_chunks
    )

class EditFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="起始行号，从1开始。替换/删除时为范围起点；插入时内容插入到该行之前（传总行数+1追加到末尾）。")
    new_text: str = Field("", description="新内容。非空时写入文件；为空时配合 end_line 表示删除。")
    end_line: Optional[int] = Field(None, description="结束行号（包含该行）。传值时为替换/删除的范围终点；不传时为插入模式。")

@tool(model=EditFileLines, description="按行号编辑文件：替换行范围(new_text+end_line)、插入(new_text，不传end_line)、删除(end_line，new_text为空)。",
      policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL, WRITE_FILE_PATH, False, "编辑文件行：{file_path}，行号：{start_line}"), feature="file")
def edit_file_lines(file_path: str, start_line: int, agent: Agent, authorization,
                    new_text: str = "", end_line: int | None = None) -> str:
    """按行号编辑文件，支持替换、插入和删除三种模式。"""
    return agent._file_mgr.edit_file_lines(
        file_path, start_line, authorization, new_text, end_line
    )

class ReplaceAllInFile(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    old_text: str = Field(..., description="要查找的原文本，必须与文件内容完全一致。")
    new_text: str = Field(..., description="替换后的新文本。")

@tool(model=ReplaceAllInFile, description="全局替换文件中所有匹配的文本。适合重命名变量、更新路径等批量替换场景。",
      policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL, WRITE_FILE_PATH, False, "全局替换文件文本：{file_path}"), feature="file")
def replace_all_in_file(file_path: str, old_text: str, new_text: str,
                        agent: Agent, authorization) -> str:
    """替换文件中所有匹配的文本。"""
    return agent._file_mgr.replace_all_in_file(
        file_path, old_text, new_text, authorization
    )
