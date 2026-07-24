from __future__ import annotations
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional

from src.mgr.permission_mgr import PermissionCheckResult, PermissionContext
from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent


# 安全关键文件名集合 — 写入这些文件始终需要人工确认（判官不得静默放行），防自我提权。
SENSITIVE_NAMES = {
    # 环境变量 / 凭证
    ".env", ".env.local", ".env.production", "credentials.json", ".npmrc", ".pypirc",
    # Shell 配置
    ".bashrc", ".zshrc", ".bash_profile", ".profile", ".zprofile",
    # Git 配置
    ".gitconfig",
}

# 安全关键的 .agent 核心配置文件名 — 仅当父目录名为 ".agent" 时命中，故项目
# <workdir>/.agent/ 与全局 ~/.agent/ 两根完全对等；角色目录下嵌套的同名文件（父目录非 .agent）不算。
SECURITY_CRITICAL_AGENT_FILES = {"settings.json", "mcp_servers.json", "config.yaml"}

# 安全关键目录前缀 — 路径中包含这些目录时需要人工确认（IDE 配置）。
SENSITIVE_DIRS = {"/.vscode/", "/.idea/"}


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


def is_security_critical_path(path: str) -> bool:
    """判断文件路径是否为安全关键路径（写入须人工确认，判官不得静默放行）。

    与所处根目录无关：项目 <workdir>/.agent/ 与全局 ~/.agent/ 待遇完全对等。命中条件（任一）：
    敏感文件名（.env/凭证/shell 配置/git 配置，任意位置）；.agent 目录下的核心配置
    （settings.json/mcp_servers.json/config.yaml，父目录名须为 .agent）；.git 内部；IDE 配置目录
    （.vscode/、.idea/）。仅按 ~ 展开归一，不做 .resolve() 以免依赖当前工作目录。

    Args:
        path: 待检查的文件路径。

    Returns:
        True 表示路径安全关键，需要人工确认。
    """
    if not path:
        return False
    try:
        expanded = Path(path).expanduser()
    except (OSError, ValueError, RuntimeError):
        return False
    name = expanded.name
    # 敏感文件名（任意位置，大小写不敏感）
    if name.lower() in SENSITIVE_NAMES:
        return True
    # 两根 .agent 核心配置：文件名 + 父目录名 == ".agent"（大小写敏感，与磁盘上实际目录名一致）
    if name in SECURITY_CRITICAL_AGENT_FILES and expanded.parent.name == ".agent":
        return True
    lower = expanded.as_posix().lower()
    # .git 目录（大小写不敏感，兼容 macOS APFS 等大小写不敏感文件系统）
    if "/.git/" in lower or lower.endswith("/.git"):
        return True
    # IDE 配置目录（.vscode/、.idea/）
    for sensitive_dir in SENSITIVE_DIRS:
        if sensitive_dir in lower or lower.endswith(sensitive_dir.rstrip("/")):
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


def _classify_edit_path(path: str, ctx: PermissionContext) -> PermissionCheckResult | None:
    """对单个编辑目标路径分级，供文件编辑/移动检查复用。

    安全关键路径 → bypass_immune 的 ask（人工确认，auto 模式下判官不接管）；工作区及可信目录外
    → 非 immune 的 ask（auto 模式下落入判官）；否则 None（工作区内非关键，交后续流程放行）。

    Args:
        path: 待分级的文件路径。
        ctx: 权限上下文，包含工作目录与可信目录。

    Returns:
        命中安全关键或工作区外时返回对应 PermissionCheckResult，否则 None。
    """
    if not path:
        return None
    if is_security_critical_path(path):
        return PermissionCheckResult("ask", f"安全关键路径需人工确认：{path}", bypass_immune=True)
    if is_outside_workspace(path, ctx.workdir, ctx.trusted_dirs):
        return PermissionCheckResult("ask", f"工作目录外路径需确认：{path}", bypass_immune=False)
    return None


def check_file_edit_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """文件编辑安全检查：安全关键路径强制人工确认，工作区外路径需确认。

    模式策略由 PermissionManager._mode_default() 统一处理。

    Args:
        tool_input: 工具调用参数。
        ctx: 权限上下文，包含当前模式、工作目录、工具名和全局配置目录。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    result = _classify_edit_path(_extract_edit_path(tool_input, ctx), ctx)
    return result if result is not None else PermissionCheckResult("passthrough")


def check_file_move_permissions(tool_input: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """move_file 安全检查：同时检查源路径和目标路径的安全关键性与工作区归属。

    Args:
        tool_input: 工具调用参数，需包含 "source" 和 "destination" 字段。
        ctx: 权限上下文，包含当前模式、工作目录、工具名和全局配置目录。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    for key in ("source", "destination"):
        result = _classify_edit_path(tool_input.get(key, ""), ctx)
        if result is not None:
            return result
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
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="列出目录：{path}", check_permissions=check_file_read_permissions), feature="file")
def list_directory(path: str | None, agent: Agent, max_depth: int = 3) -> str:
    return agent._file_mgr.list_directory(
        path or str(agent._file_mgr.workdir),
        max_depth=max_depth,
    )

class Glob(BaseModel):
    pattern: str = Field(..., description="文件名/路径 glob，默认递归匹配，如 '*.py'、'**/config*.yaml'。只匹配路径，不读取内容。")
    path: Optional[str] = Field(None, description="查找起点目录绝对路径，不提供时默认为工作目录。")

@tool(model=Glob, description="用 ripgrep 按 glob 查找文件，遵守 .gitignore、排除隐藏文件，只返回文件（不含目录）。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="在 {path} 中查找：{pattern}", check_permissions=check_file_read_permissions), feature="file")
def glob(pattern: str, agent: Agent, path: str | None = None) -> str:
    return agent._file_mgr.glob(pattern, path=path or str(agent._file_mgr.workdir))

class Grep(BaseModel):
    pattern: str = Field(..., description="ripgrep 正则表达式，默认区分大小写；搜字面符号 . ( [ * 等需转义。不是 glob。")
    path: Optional[str] = Field(None, description="目录或文件的绝对路径。为目录时递归搜索其中所有文件内容。不提供时默认为工作目录。不支持 glob。")

@tool(model=Grep, description="用 ripgrep 正则搜索文件内容，返回匹配的文件、行号和匹配行，遵守 .gitignore。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="搜索内容：{pattern}", check_permissions=check_file_read_permissions), feature="file")
def grep(pattern: str, agent: Agent, path: str | None = None) -> str:
    return agent._file_mgr.grep(pattern, path=path or str(agent._file_mgr.workdir))

class GetFileInfo(BaseModel):
    path: str = Field(..., description="要查询的文件或目录的绝对路径。")

@tool(model=GetFileInfo, description="获取文件或目录的详细元数据，包括大小、行数、时间、权限等。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="查看文件信息：{path}", check_permissions=check_file_read_permissions), feature="file")
def get_file_info(path: str, agent: Agent) -> str:
    return agent._file_mgr.get_file_info(path)

class ReadFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    start_line: Optional[int] = Field(None, description="起始行号，从1开始；未提供时从文件开头读取。")
    end_line: Optional[int] = Field(None, description="结束行号，包含该行；未提供时读取到文件末尾。")

@tool(model=ReadFile, description="读取文件内容并附带行号，可指定行数范围。",
      permission=ToolPermission(kind="readonly", specifier_arg="path", tips="读取文件：{path}", check_permissions=check_file_read_permissions), feature="file")
def read_file(path: str, agent: Agent,
              start_line: int | None = None,
              end_line: int | None = None) -> str:
    return agent._file_mgr.read_file(path, start_line=start_line, end_line=end_line)

class CreateDirectory(BaseModel):
    path: str = Field(..., description="要创建的目录的绝对路径，支持多级目录。")

@tool(model=CreateDirectory, description="创建新目录。",
      permission=ToolPermission(kind="edit", specifier_arg="path", tips="创建目录：{path}", check_permissions=check_file_edit_permissions), feature="file")
def create_directory(path: str, agent: Agent) -> str:
    return agent._file_mgr.create_directory(path)

class MoveFile(BaseModel):
    source: str = Field(..., description="源文件或目录的绝对路径。")
    destination: str = Field(..., description="目标绝对路径。若目标是已有目录，则移入该目录内。")

@tool(model=MoveFile, description="移动或重命名文件/目录。",
      permission=ToolPermission(kind="edit", specifier_arg="source", tips="移动或重命名：{source} -> {destination}", check_permissions=check_file_move_permissions), feature="file")
def move_file(source: str, destination: str, agent: Agent) -> str:
    return agent._file_mgr.move_file(source, destination)

class WriteFile(BaseModel):
    path: str = Field(..., description="文件绝对路径。")
    content: str = Field(..., description="要写入的内容。")
    append: bool = Field(False, description="True=追加到文件末尾，False=覆盖写入。")
    chunk_index: Optional[int] = Field(None, description="当前分块序号（从1开始），用于分块写入大文件。")
    total_chunks: Optional[int] = Field(None, description="总分块数，与 chunk_index 配合使用。")

@tool(model=WriteFile, description="新建、覆盖写入或追加完整文件内容，支持分块写入大文件。不用精确编辑文件内容",
      permission=ToolPermission(kind="edit", specifier_arg="path", tips="写入文件：{path}", check_permissions=check_file_edit_permissions), feature="file")
def write_file(path: str, content: str, agent: Agent,
               append: bool = False,
               chunk_index: int | None = None,
               total_chunks: int | None = None) -> str:
    return agent._file_mgr.write_file(path, content, append, chunk_index, total_chunks)

class EditFileLines(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    start_line: int = Field(..., description="起始行号，从1开始。替换/删除时为范围起点；插入时内容插入到该行之前（传总行数+1追加到末尾）。")
    new_text: str = Field("", description="新内容。非空时写入文件；为空时配合 end_line 表示删除。")
    end_line: Optional[int] = Field(None, description="结束行号（包含该行）。传值时为替换/删除的范围终点；不传时为插入模式。")

@tool(model=EditFileLines, description="按行号编辑文件：替换行范围(new_text+end_line)、插入(new_text，不传end_line)、删除(end_line，new_text为空)。",
      permission=ToolPermission(kind="edit", specifier_arg="file_path", tips="编辑文件行：{file_path}，行号：{start_line}", check_permissions=check_file_edit_permissions), feature="file")
def edit_file_lines(file_path: str, start_line: int, agent: Agent,
                    new_text: str = "", end_line: int | None = None) -> str:
    """按行号编辑文件，支持替换、插入和删除三种模式。"""
    return agent._file_mgr.edit_file_lines(file_path, start_line, new_text, end_line)

class ReplaceAllInFile(BaseModel):
    file_path: str = Field(..., description="要编辑的文件绝对路径。")
    old_text: str = Field(..., description="要查找的原文本，必须与文件内容完全一致。")
    new_text: str = Field(..., description="替换后的新文本。")

@tool(model=ReplaceAllInFile, description="全局替换文件中所有匹配的文本。适合重命名变量、更新路径等批量替换场景。",
      permission=ToolPermission(kind="edit", specifier_arg="file_path", tips="全局替换文件文本：{file_path}", check_permissions=check_file_edit_permissions), feature="file")
def replace_all_in_file(file_path: str, old_text: str, new_text: str,
                        agent: Agent) -> str:
    """替换文件中所有匹配的文本。"""
    return agent._file_mgr.replace_all_in_file(file_path, old_text, new_text)
