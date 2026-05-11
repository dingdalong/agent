from __future__ import annotations
from typing import TYPE_CHECKING, Optional

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class ListDirectory(BaseModel):
    path: str = Field(".", description="相对目录路径，默认为当前工作目录。")
    recursive: bool = Field(False, description="是否递归列出子目录内容。")
    max_depth: int = Field(3, description="递归最大深度，仅在 recursive=True 时生效。")

@tool(model=ListDirectory, description="列出目录内容，显示文件和子目录的树形结构。")
async def list_directory(path: str, agent: Agent,
                         recursive: bool = False, max_depth: int = 3) -> str:
    return await agent._file_mgr.list_directory(
        path,
        recursive=recursive,
        max_depth=max_depth,
    )

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的相对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。")
async def create_directory(path: str, agent: Agent) -> str:
    return await agent._file_mgr.create_directory(path)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的相对路径。")
    destination: str = Field(..., description="目标相对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。")
async def move_file(source: str, destination: str, agent: Agent) -> str:
    return await agent._file_mgr.move_file(source, destination)

class FindFiles(BaseModel):
    pattern: str = Field(..., description="glob匹配模式，如 '*.py'、'**/*.json'。")
    path: str = Field(".", description="搜索起点的相对路径，默认为当前工作目录。")

@tool(model=FindFiles, description="按glob模式搜索文件。")
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

@tool(model=SearchFiles, description="全局搜索文件内容，类似 VS Code Search，支持文本/正则、大小写、全词、包含/排除glob。")
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

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。")
async def get_file_info(path: str, agent: Agent) -> str:
    return await agent._file_mgr.get_file_info(path)

class ReadFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")

@tool(model=ReadFile, description="读取文件内容并附带行号，便于后续精确编辑。")
async def read_file(path: str, agent: Agent) -> str:
    return await agent._file_mgr.read_file(path)

class WriteFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="写入或追加文件内容，支持分块写入大文件。")
async def write_file(path: str, content: str, agent: Agent,
                     append: bool = False,
                     chunk_index: int | None = None,
                     total_chunks: int | None = None) -> str:
    return await agent._file_mgr.write_file(path, content, append, chunk_index, total_chunks)

class EditFile(BaseModel):
    path: str = Field(..., description="相对文件路径。")
    mode: str = Field("replace", description=(
        "编辑模式: "
        "replace=查找替换old_text为new_text; "
        "range_replace=将start_line到end_line的内容替换为new_text; "
        "insert=在start_line行前插入new_text; "
        "delete=删除start_line到end_line的行。"
    ))
    old_text: Optional[str] = Field(None, description="要查找的文本 (replace 模式必填)。")
    new_text: Optional[str] = Field(None, description="替换/插入的新内容。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始 (range_replace/insert/delete 模式使用)。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行 (range_replace/delete 模式使用)。")
    count: int = Field(1, description="replace 模式下的替换次数，0=全部替换。")

@tool(model=EditFile, description="精确编辑文件：支持查找替换、按行范围替换、插入、删除四种模式。")
async def edit_file(path: str, agent: Agent,
                    mode: str = "replace",
                    old_text: str | None = None,
                    new_text: str | None = None,
                    start_line: int | None = None,
                    end_line: int | None = None,
                    count: int = 1) -> str:
    return await agent._file_mgr.edit_file(path, mode, old_text, new_text, start_line, end_line, count)
