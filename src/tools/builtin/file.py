from __future__ import annotations
from pathlib import Path, PurePosixPath
from typing import Any, TYPE_CHECKING, Optional

from src.mgr.permission_mgr import PermissionCheckResult, PermissionContext
from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent


# 敏感文件名集合 — 写入这些文件始终需要用户确认（对齐 CC v2.1.173 的 checkPathSafetyForAutoEdit）
SENSITIVE_NAMES = {
    # 环境变量 / 凭证
    ".env", ".env.local", ".env.production", "credentials.json", ".npmrc", ".pypirc",
    # Shell 配置
    ".bashrc", ".zshrc", ".bash_profile", ".profile", ".zprofile",
    # Git 配置
    ".gitconfig",
}

# 敏感目录前缀 — 路径中包含这些目录时需要用户确认
SENSITIVE_DIRS = {"/.agent/", "/.vscode/", "/.idea/"}


def is_outside_workspace(path: str, workdir: str, extra_trusted: tuple[str, ...] = ()) -> bool:
    """判断文件路径是否在工作目录及可信目录之外。

    Args:
        path: 待检查的文件路径。
        workdir: 工作区根目录路径。
        extra_trusted: 额外可信目录路径列表（如 global_dir），这些目录内的路径视同工作目录内。

    Returns:
        True 表示路径在所有可信目录外。
    """
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
        workdir_resolved = Path(workdir).resolve()
        if resolved.is_relative_to(workdir_resolved):
            return False
        for trusted in extra_trusted:
            if trusted and resolved.is_relative_to(Path(trusted).resolve()):
                return False
        return True
    except (OSError, ValueError):
        return True


def is_sensitive_path(path: str, workdir: str, extra_trusted: tuple[str, ...] = ()) -> bool:
    """判断文件路径是否为敏感路径（.git 目录、敏感配置文件、敏感目录、可信目录外路径）。

    Args:
        path: 待检查的文件路径。
        workdir: 工作区根目录路径。
        extra_trusted: 额外可信目录路径列表（如 global_dir），这些目录内的路径不视为"外部路径"。

    Returns:
        True 表示路径敏感，需要用户确认。
    """
    if not path:
        return False
    normalized = PurePosixPath(path).as_posix()
    lower = normalized.lower()
    # .git 目录检查（大小写不敏感，兼容 macOS APFS 等大小写不敏感文件系统）
    if "/.git/" in lower or lower.endswith("/.git"):
        return True
    if PurePosixPath(path).name.lower() in SENSITIVE_NAMES:
        return True
    # 敏感目录检查（.agent/、.vscode/、.idea/ 等项目配置目录，大小写不敏感）
    for sensitive_dir in SENSITIVE_DIRS:
        if sensitive_dir in lower or lower.endswith(sensitive_dir.rstrip("/")):
            return True
    if is_outside_workspace(path, workdir, extra_trusted):
        return True
    return False


def _extract_edit_path(tool_input: dict[str, Any], ctx: PermissionContext) -> str:
    """从工具调用参数中提取文件编辑路径。

    优先使用 ctx.specifier_arg 指定的参数名，兜底按常见参数名查找。

    Args:
        tool_input: 工具调用参数。
        ctx: 权限上下文。

    Returns:
        提取到的路径字符串，未找到时返回空字符串。
    """
    if ctx.specifier_arg:
        value = tool_input.get(ctx.specifier_arg, "")
        if isinstance(value, str) and value:
            return value
    return tool_input.get("path") or tool_input.get("file_path") or tool_input.get("source") or ""


def check_file_edit_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """文件编辑安全检查：敏感路径强制询问。模式策略由 PermissionManager._mode_default() 统一处理。

    Args:
        tool_input: 工具调用参数。
        ctx: 权限上下文，包含当前模式、工作目录、工具名和全局配置目录。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    path = _extract_edit_path(tool_input, ctx)
    if is_sensitive_path(path, ctx.workdir, ctx.trusted_dirs):
        return PermissionCheckResult("ask", f"敏感路径需确认：{path}", bypass_immune=True)
    return PermissionCheckResult("passthrough")


def check_file_move_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """move_file 安全检查：同时检查源路径和目标路径的敏感性。

    Args:
        tool_input: 工具调用参数，需包含 "source" 和 "destination" 字段。
        ctx: 权限上下文，包含当前模式、工作目录、工具名和全局配置目录。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    for key in ("source", "destination"):
        path = tool_input.get(key, "")
        if is_sensitive_path(path, ctx.workdir, ctx.trusted_dirs):
            return PermissionCheckResult("ask", f"敏感路径需确认：{path}", bypass_immune=True)
    return PermissionCheckResult("passthrough")


def check_file_read_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """只读文件工具安全检查：工作目录及可信目录外的路径需用户确认。

    仅检查路径是否在工作区和可信目录外，不检查敏感文件名。
    模式策略由 PermissionManager._mode_default() 统一处理。

    Args:
        tool_input: 工具调用参数。
        ctx: 权限上下文，包含当前模式、工作目录、工具名和全局配置目录。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    path = _extract_edit_path(tool_input, ctx)
    if is_outside_workspace(path, ctx.workdir, ctx.trusted_dirs):
        return PermissionCheckResult("ask", f"工作目录外路径需确认：{path}", bypass_immune=False)
    return PermissionCheckResult("passthrough")


class ListDirectory(BaseModel):
    path: Optional[str] = Field(None, description="目录绝对路径，不提供时默认为工作目录。")
    max_depth: Optional[int] = Field(3, description="递归列出子目录的最大深度，默认为3。")

@tool(model=ListDirectory, description="列出目录结构，显示文件和子目录的树形结构。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="列出目录：{path}", check_permissions=check_file_read_permissions))
async def list_directory(path: str | None, agent: Agent, max_depth: int = 3) -> str:
    return await agent._file_mgr.list_directory(
        path or str(agent._file_mgr.workdir),
        max_depth=max_depth,
    )

class FindFiles(BaseModel):
    pattern: str = Field(..., description="文件名或目录 glob，只匹配路径，如 '*.py'、'**/config*.yaml'")
    path: Optional[str] = Field(None, description="查找起点目录绝对路径，不提供时默认为工作目录。")

@tool(model=FindFiles, description="查找文件或目录，支持glob。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="在 {path} 中查找：{pattern}", check_permissions=check_file_read_permissions))
async def find_files(pattern: str, agent: Agent, path: str | None = None) -> str:
    return await agent._file_mgr.find_files(pattern, path=path or str(agent._file_mgr.workdir))

class SearchFiles(BaseModel):
    query: str = Field(..., description="要查找的普通文本，不区分大小写；不是 glob，也不是正则。")
    path: Optional[str] = Field(None, description="目录或文件的绝对路径。当为目录时，搜索目录中所有文件内容，包括子目录中的文件。不提供时默认为工作目录。不支持 glob。")

@tool(model=SearchFiles, description="搜索文本内容，返回匹配文件、行号和匹配行。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="搜索文本：{query}", check_permissions=check_file_read_permissions))
async def search_files(query: str, agent: Agent, path: str | None = None) -> str:
    return await agent._file_mgr.search_files(query, path=path or str(agent._file_mgr.workdir))

class GetFileInfo(BaseModel):
    path: str = Field(..., description="要查询的文件或目录的绝对路径。")

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="查看文件信息：{path}", check_permissions=check_file_read_permissions))
async def get_file_info(path: str, agent: Agent) -> str:
    return await agent._file_mgr.get_file_info(path)

class ReadFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始；未提供时从文件开头读取。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行；未提供时读取到文件末尾。")

@tool(model=ReadFile, description="读取文件内容并附带行号，可指定行数范围。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="读取文件：{path}", check_permissions=check_file_read_permissions))
async def read_file(path: str, agent: Agent,
                    start_line: int | None = None,
                    end_line: int | None = None) -> str:
    return await agent._file_mgr.read_file(path, start_line=start_line, end_line=end_line)

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的绝对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。",
      permission=ToolPermission(kind="edit", specifier_arg="path", tips="创建目录：{path}", check_permissions=check_file_edit_permissions))
async def create_directory(path: str, agent: Agent) -> str:
    return await agent._file_mgr.create_directory(path)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的绝对路径。")
    destination: str = Field(..., description="目标绝对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。",
      permission=ToolPermission(kind="edit", specifier_arg="source", tips="移动或重命名：{source} -> {destination}", check_permissions=check_file_move_permissions))
async def move_file(source: str, destination: str, agent: Agent) -> str:
    return await agent._file_mgr.move_file(source, destination)

class WriteFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="新建、覆盖写入或追加完整文件内容，支持分块写入大文件。不用精确编辑文件内容",
      permission=ToolPermission(kind="edit", specifier_arg="path", tips="写入文件：{path}", check_permissions=check_file_edit_permissions))
async def write_file(path: str, content: str, agent: Agent,
                     append: bool = False,
                     chunk_index: int | None = None,
                     total_chunks: int | None = None) -> str:
    return await agent._file_mgr.write_file(path, content, append, chunk_index, total_chunks)

class EditFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="起始行号，从1开始。替换/删除时为范围起点；插入时内容插入到该行之前（传总行数+1追加到末尾）。")
    new_text: str = Field("", description="新内容。非空时写入文件；为空时配合 end_line 表示删除。")
    end_line: Optional[int] = Field(None, description="结束行号（包含该行）。传值时为替换/删除的范围终点；不传时为插入模式。")

@tool(model=EditFileLines, description="按行号编辑文件：替换行范围(new_text+end_line)、插入(new_text，不传end_line)、删除(end_line，new_text为空)。",
      permission=ToolPermission(kind="edit", specifier_arg="file_path", tips="编辑文件行：{file_path}，行号：{start_line}", check_permissions=check_file_edit_permissions))
async def edit_file_lines(file_path: str, start_line: int, agent: Agent,
                          new_text: str = "", end_line: int | None = None) -> str:
    """按行号编辑文件，支持替换、插入和删除三种模式。"""
    return await agent._file_mgr.edit_file_lines(file_path, start_line, new_text, end_line)

class ReplaceAllInFile(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    old_text: str = Field(..., description="要查找的原文本，必须与文件内容完全一致。")
    new_text: str = Field(..., description="替换后的新文本。")

@tool(model=ReplaceAllInFile, description="全局替换文件中所有匹配的文本。适合重命名变量、更新路径等批量替换场景。",
      permission=ToolPermission(kind="edit", specifier_arg="file_path", tips="全局替换文件文本：{file_path}", check_permissions=check_file_edit_permissions))
async def replace_all_in_file(file_path: str, old_text: str, new_text: str,
                              agent: Agent) -> str:
    """替换文件中所有匹配的文本。"""
    return await agent._file_mgr.replace_all_in_file(file_path, old_text, new_text)
