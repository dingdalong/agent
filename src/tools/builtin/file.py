from __future__ import annotations
from typing import TYPE_CHECKING, Optional

from src.tools.decorator import PermissionRule, ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class ListDirectory(BaseModel):
    path: str = Field(".", description="相对目录路径，默认为当前工作目录。")
    recursive: bool = Field(False, description="是否递归列出子目录内容。")
    max_depth: int = Field(3, description="递归最大深度，仅在 recursive=True 时生效。")

@tool(model=ListDirectory, description="列出目录内容，显示文件和子目录的树形结构。",
      permission=ToolPermission(tips="列出目录：{path}", args=["path"], rules=[PermissionRule(permission="allow")]))
async def list_directory(path: str, agent: Agent,
                         recursive: bool = False, max_depth: int = 3) -> str:
    return await agent._file_mgr.list_directory(
        path,
        recursive=recursive,
        max_depth=max_depth,
    )

class FindFiles(BaseModel):
    pattern: str = Field(..., description="glob匹配模式，如 '*.py'、'**/*.json'。")
    path: str = Field(".", description="搜索起点的相对路径，默认为当前工作目录。")

@tool(model=FindFiles, description="按glob模式搜索文件。",
      permission=ToolPermission(tips="在 {path} 中查找：{pattern}", args=["path"], rules=[PermissionRule(permission="allow")]))
async def find_files(pattern: str, agent: Agent, path: str = ".") -> str:
    return await agent._file_mgr.find_files(pattern, path=path)

class SearchFiles(BaseModel):
    query: str = Field(..., description="要搜索的文本或正则表达式。")
    include: Optional[str] = Field(None, description="包含的glob模式，多个用英文逗号分隔；可用于限定目录，如 'src/**,**/*.md'。")
    exclude: Optional[str] = Field(None, description="额外排除的glob模式，多个用英文逗号分隔，如 '.git/**,node_modules/**,*.lock'。")
    use_regex: Optional[bool] = Field(False, description="是否将 query 作为正则表达式；默认 False 表示普通文本搜索。")
    match_case: Optional[bool] = Field(False, description="是否区分大小写。")
    match_whole_word: Optional[bool] = Field(False, description="是否全词匹配。")
    include_ignored: Optional[bool] = Field(False, description="是否搜索被 .gitignore 忽略的文件；默认 False，主动查询忽略内容时设为 True。")

@tool(model=SearchFiles, description="全局搜索文件内容，类似 VS Code Search，支持文本/正则、大小写、全词、包含/排除glob。",
      permission=ToolPermission(tips="搜索文本：{query}", rules=[PermissionRule(permission="allow")]))
async def search_files(query: str, agent: Agent,
                       include: str | None = None,
                       exclude: str | None = None,
                       use_regex: bool | None = False,
                       match_case: bool | None = False,
                       match_whole_word: bool | None = False,
                       include_ignored: bool | None = False) -> str:
    return await agent._file_mgr.search_files(
        query,
        include=include,
        exclude=exclude,
        use_regex=bool(use_regex),
        match_case=bool(match_case),
        match_whole_word=bool(match_whole_word),
        include_ignored=bool(include_ignored),
    )

class GetFileInfo(BaseModel):
    path: str = Field(..., description="要查询的文件或目录的相对路径。")

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。",
      permission=ToolPermission(tips="查看文件信息：{path}", args=["path"], rules=[PermissionRule(permission="allow")]))
async def get_file_info(path: str, agent: Agent) -> str:
    return await agent._file_mgr.get_file_info(path)

class ReadFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始；未提供时从文件开头读取。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行；未提供时读取到文件末尾。")

@tool(model=ReadFile, description="读取文件内容并附带行号，可指定行数范围，便于后续精确编辑。",
      permission=ToolPermission(tips="读取文件：{path}", args=["path"], rules=[PermissionRule(permission="allow")]))
async def read_file(path: str, agent: Agent,
                    start_line: int | None = None,
                    end_line: int | None = None) -> str:
    return await agent._file_mgr.read_file(path, start_line=start_line, end_line=end_line)

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的相对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。",
      permission=ToolPermission(tips="创建目录：{path}", args=["path"]))
async def create_directory(path: str, agent: Agent) -> str:
    return await agent._file_mgr.create_directory(path)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的相对路径。")
    destination: str = Field(..., description="目标相对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。",
      permission=ToolPermission(
          tips="移动或重命名：{source} -> {destination}",
          args=["source", "destination"],
      ))
async def move_file(source: str, destination: str, agent: Agent) -> str:
    return await agent._file_mgr.move_file(source, destination)

class WriteFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="新建、覆盖写入或追加完整文件内容，支持分块写入大文件；局部精确编辑请使用文件编辑工具。",
      permission=ToolPermission(tips="写入文件：{path}", args=["path"]))
async def write_file(path: str, content: str, agent: Agent,
                     append: bool = False,
                     chunk_index: int | None = None,
                     total_chunks: int | None = None) -> str:
    return await agent._file_mgr.write_file(path, content, append, chunk_index, total_chunks)

class ReplaceInFile(BaseModel):
    file_path: str = Field(..., description="要编辑的相对文件路径。")
    old_text: str = Field(..., description="要精确查找的原文本，必须与文件内容完全一致。")
    new_text: str = Field(..., description="替换后的新文本；传空字符串表示删除匹配文本。")
    count: int = Field(1, description="替换次数，0=全部替换。")

@tool(model=ReplaceInFile, description="精确替换文件中的文本。用于已知 old_text 完整内容时的局部编辑；count=0 表示全部替换。",
      permission=ToolPermission(tips="替换文件文本：{file_path}", args=["file_path"]))
async def replace_in_file(file_path: str, old_text: str, new_text: str,
                          agent: Agent, count: int = 1) -> str:
    return await agent._file_mgr.replace_in_file(file_path, old_text, new_text, count)

class ReplaceFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的相对文件路径。")
    start_line: int = Field(..., description="要替换的起始行号，从1开始。")
    end_line: int = Field(..., description="要替换的结束行号，包含该行。")
    new_text: str = Field(..., description="用于替换该行范围的新内容。")

@tool(model=ReplaceFileLines, description="按行号范围替换文件内容。适合 read_file 返回行号后进行精确行级编辑。",
      permission=ToolPermission(tips="替换文件行：{file_path}，行范围：{start_line}-{end_line}", args=["file_path"]))
async def replace_file_lines(file_path: str, start_line: int, end_line: int,
                             new_text: str, agent: Agent) -> str:
    return await agent._file_mgr.replace_file_lines(file_path, start_line, end_line, new_text)

class InsertFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的相对文件路径。")
    start_line: int = Field(..., description="插入位置，从1开始；内容会插入到该行之前。传总行数+1表示追加到末尾。")
    new_text: str = Field(..., description="要插入的新内容。")

@tool(model=InsertFileLines, description="在指定行之前插入文件内容。传 start_line=总行数+1 可追加到文件末尾。",
      permission=ToolPermission(tips="插入文件行：{file_path}，位置：{start_line}", args=["file_path"]))
async def insert_file_lines(file_path: str, start_line: int,
                            new_text: str, agent: Agent) -> str:
    return await agent._file_mgr.insert_file_lines(file_path, start_line, new_text)

class DeleteFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的相对文件路径。")
    start_line: int = Field(..., description="要删除的起始行号，从1开始。")
    end_line: int = Field(..., description="要删除的结束行号，包含该行。")

@tool(model=DeleteFileLines, description="按行号范围删除文件内容。适合 read_file 返回行号后进行精确删除。",
      permission=ToolPermission(tips="删除文件行：{file_path}，行范围：{start_line}-{end_line}", args=["file_path"]))
async def delete_file_lines(file_path: str, start_line: int,
                            end_line: int, agent: Agent) -> str:
    return await agent._file_mgr.delete_file_lines(file_path, start_line, end_line)
