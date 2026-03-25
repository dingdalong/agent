import os
import re
from pathlib import Path
from . import tool
from pydantic import BaseModel, Field
from typing import Optional

# === 安全工具函数 ===

def _safe_path(filename: str) -> tuple[str, str | None]:
    """校验并返回安全的绝对路径，失败返回 ("", 错误信息)"""
    workspace = os.path.abspath("./workspace")
    os.makedirs(workspace, exist_ok=True)
    full_path = os.path.abspath(os.path.join(workspace, filename))
    if not full_path.startswith(workspace):
        return ("", "错误：文件名不能包含路径遍历")
    return (full_path, None)


def _read_lines(full_path: str) -> tuple[list[str], str | None]:
    """读取文件所有行，返回 (行列表, 错误信息)"""
    if not os.path.isfile(full_path):
        return ([], f"错误：文件不存在")
    with open(full_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return (lines, None)


def _write_lines(full_path: str, lines: list[str]) -> None:
    """将行列表写回文件"""
    with open(full_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _validate_line_range(lines: list[str], start: int, end: int) -> str | None:
    """校验行号范围，返回错误信息或 None"""
    total = len(lines)
    if start < 1 or end < 1:
        return f"错误：行号必须 >= 1"
    if start > total or end > total:
        return f"错误：行号超出范围（文件共 {total} 行）"
    if start > end:
        return f"错误：起始行 {start} 不能大于结束行 {end}"
    return None


# === 参数模型 ===

class WriteFileArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    content: str = Field(description="文件内容")

class ReadFileArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    show_line_numbers: bool = Field(default=False, description="是否显示行号")
    start_line: Optional[int] = Field(default=None, description="起始行号（从 1 开始），不填则从头开始")
    end_line: Optional[int] = Field(default=None, description="结束行号（闭区间），不填则读到末尾")

class DeleteFileArgs(BaseModel):
    filename: str = Field(description="要删除的文件名（相对于 workspace 目录）")

class ListFilesArgs(BaseModel):
    subdir: str = Field(default="", description="子目录路径，默认列出 workspace 根目录")
    recursive: bool = Field(default=False, description="是否递归列出所有子目录中的文件")

class AppendFileArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    content: str = Field(description="要追加的内容")

class SearchFileArgs(BaseModel):
    keyword: str = Field(description="要搜索的关键词或正则表达式")
    filename: Optional[str] = Field(default=None, description="指定文件名搜索，为空则搜索整个 workspace")
    use_regex: bool = Field(default=False, description="是否使用正则表达式匹配，默认为普通文本搜索")

class InsertLinesArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    line_number: int = Field(description="在此行之前插入内容（从 1 开始，等于总行数+1 则追加到末尾）")
    content: str = Field(description="要插入的文本内容")

class DeleteLinesArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    start_line: int = Field(description="起始行号（从 1 开始，闭区间）")
    end_line: int = Field(description="结束行号（闭区间，包含此行）")

class ReplaceLinesArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    start_line: int = Field(description="起始行号（从 1 开始，闭区间）")
    end_line: int = Field(description="结束行号（闭区间，包含此行）")
    content: str = Field(description="用于替换的新内容")

class FindFilesArgs(BaseModel):
    pattern: str = Field(description="文件名匹配模式，支持 glob 语法（如 '*.py', '**/*.txt'）")
    keyword: Optional[str] = Field(default=None, description="可选：只返回内容包含此关键词的文件")

class FindReplaceArgs(BaseModel):
    filename: str = Field(description="文件名（相对于 workspace 目录）")
    old_text: str = Field(description="要查找的文本")
    new_text: str = Field(description="替换为的新文本")
    replace_all: bool = Field(default=False, description="是否替换所有匹配项，默认只替换第一个")


# === 工具函数 ===

@tool(model=WriteFileArgs, description="创建或覆盖写入文件到工作区", sensitive=True,
      confirm_template="将内容写入文件 '{filename}'")
async def write_file(filename: str, content: str) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"文件已保存：{filename}"


@tool(model=ReadFileArgs, description="读取工作区中的文件内容，支持行号显示和范围读取")
async def read_file(filename: str, show_line_numbers: bool = False,
                    start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    lines, err = _read_lines(full_path)
    if err:
        return f"错误：文件 '{filename}' 不存在"
    if not lines:
        return f"文件 '{filename}' 内容为空"

    total = len(lines)

    # 处理范围参数
    s = (start_line or 1)
    e = (end_line or total)
    range_err = _validate_line_range(lines, s, e)
    if range_err:
        return range_err

    selected = lines[s - 1:e]

    if show_line_numbers:
        width = len(str(e))
        output = []
        for i, line in enumerate(selected, s):
            output.append(f"{i:>{width}}: {line.rstrip()}")
        return "\n".join(output)
    else:
        return "".join(selected)


@tool(model=DeleteFileArgs, description="删除工作区中的文件", sensitive=True,
      confirm_template="删除文件 '{filename}'")
async def delete_file(filename: str) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    if not os.path.exists(full_path):
        return f"错误：文件 '{filename}' 不存在"
    if os.path.isdir(full_path):
        return f"错误：'{filename}' 是目录，不能用此工具删除"
    os.remove(full_path)
    return f"文件已删除：{filename}"


@tool(model=ListFilesArgs, description="列出工作区中的文件和目录，支持递归列出子目录")
async def list_files(subdir: str = "", recursive: bool = False) -> str:
    workspace = os.path.abspath("./workspace")
    target = os.path.abspath(os.path.join(workspace, subdir))
    if not target.startswith(workspace):
        return "错误：路径不能超出工作区范围"
    if not os.path.isdir(target):
        return f"错误：目录 '{subdir or 'workspace'}' 不存在"

    entries = []

    if recursive:
        for root, dirs, files in os.walk(target):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, target)
                size = os.path.getsize(full)
                entries.append(f"📄 {rel} ({_format_size(size)})")
            if len(entries) >= 200:
                entries.append("... 文件过多，仅显示前 200 个")
                break
    else:
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            if os.path.isdir(full):
                entries.append(f"📁 {name}/")
            else:
                size = os.path.getsize(full)
                entries.append(f"📄 {name} ({_format_size(size)})")

    if not entries:
        return "目录为空"
    return "\n".join(entries)


@tool(model=AppendFileArgs, description="向工作区文件末尾追加内容", sensitive=True,
      confirm_template="向文件 '{filename}' 追加内容")
async def append_file(filename: str, content: str) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    if not os.path.isfile(full_path):
        return f"错误：文件 '{filename}' 不存在，请先使用 write_file 创建"
    with open(full_path, "a", encoding="utf-8") as f:
        f.write(content)
    return f"内容已追加到：{filename}"


@tool(model=SearchFileArgs, description="在工作区文件中搜索关键词或正则表达式，返回匹配的行号和内容")
async def search_file(keyword: str, filename: Optional[str] = None, use_regex: bool = False) -> str:
    workspace = os.path.abspath("./workspace")

    # 正则模式下预编译
    if use_regex:
        try:
            pattern = re.compile(keyword)
        except re.error as e:
            return f"错误：正则表达式无效 - {e}"

    if filename:
        full_path, err = _safe_path(filename)
        if err:
            return err
        if not os.path.isfile(full_path):
            return f"错误：文件 '{filename}' 不存在"
        files_to_search = [(filename, full_path)]
    else:
        files_to_search = []
        for root, _, filenames in os.walk(workspace):
            for fn in filenames:
                abs_path = os.path.join(root, fn)
                rel_path = os.path.relpath(abs_path, workspace)
                files_to_search.append((rel_path, abs_path))

    results = []
    for rel_name, abs_path in files_to_search:
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    matched = pattern.search(line) if use_regex else (keyword in line)
                    if matched:
                        results.append(f"{rel_name}:{line_no}: {line.rstrip()}")
        except (UnicodeDecodeError, PermissionError):
            continue

    if not results:
        return f"未找到包含 '{keyword}' 的内容"
    if len(results) > 50:
        results = results[:50]
        results.append(f"... 结果过多，仅显示前 50 条")
    return "\n".join(results)


@tool(model=FindFilesArgs, description="按文件名模式查找工作区中的文件，支持 glob 语法如 *.py 和 **/*.txt")
async def find_files(pattern: str, keyword: Optional[str] = None) -> str:
    workspace = os.path.abspath("./workspace")
    workspace_path = Path(workspace)

    if not workspace_path.is_dir():
        return "错误：工作区目录不存在"

    matched = []
    for p in workspace_path.glob(pattern):
        # 安全检查：确保在 workspace 内
        if not str(p.resolve()).startswith(workspace):
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(workspace_path)

        # 可选内容过滤
        if keyword:
            try:
                text = p.read_text(encoding="utf-8")
                if keyword not in text:
                    continue
            except (UnicodeDecodeError, PermissionError):
                continue

        size = p.stat().st_size
        matched.append(f"📄 {rel} ({_format_size(size)})")
        if len(matched) >= 100:
            matched.append("... 结果过多，仅显示前 100 个")
            break

    if not matched:
        return f"未找到匹配 '{pattern}' 的文件"
    return f"找到 {len(matched)} 个匹配文件：\n" + "\n".join(matched)


# === 行级编辑工具 ===

@tool(model=InsertLinesArgs, description="在文件的指定行号处插入内容", sensitive=True,
      confirm_template="在文件 '{filename}' 第 {line_number} 行插入内容")
async def insert_lines(filename: str, line_number: int, content: str) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    lines, err = _read_lines(full_path)
    if err:
        return f"错误：文件 '{filename}' 不存在"

    total = len(lines)
    if line_number < 1 or line_number > total + 1:
        return f"错误：行号 {line_number} 超出范围（有效范围 1~{total + 1}）"

    # 确保插入内容以换行结尾
    if content and not content.endswith("\n"):
        content += "\n"

    new_lines = content.splitlines(keepends=True)
    lines[line_number - 1:line_number - 1] = new_lines
    _write_lines(full_path, lines)

    inserted_count = len(new_lines)
    return f"已在第 {line_number} 行处插入 {inserted_count} 行（文件现共 {len(lines)} 行）"


@tool(model=DeleteLinesArgs, description="删除文件中指定范围的行", sensitive=True,
      confirm_template="删除文件 '{filename}' 第 {start_line}~{end_line} 行")
async def delete_lines(filename: str, start_line: int, end_line: int) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    lines, err = _read_lines(full_path)
    if err:
        return f"错误：文件 '{filename}' 不存在"

    range_err = _validate_line_range(lines, start_line, end_line)
    if range_err:
        return range_err

    deleted = lines[start_line - 1:end_line]
    del lines[start_line - 1:end_line]
    _write_lines(full_path, lines)

    deleted_count = len(deleted)
    return f"已删除第 {start_line}~{end_line} 行（共 {deleted_count} 行，文件现共 {len(lines)} 行）"


@tool(model=ReplaceLinesArgs, description="将文件中指定范围的行替换为新内容", sensitive=True,
      confirm_template="替换文件 '{filename}' 第 {start_line}~{end_line} 行内容")
async def replace_lines(filename: str, start_line: int, end_line: int, content: str) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    lines, err = _read_lines(full_path)
    if err:
        return f"错误：文件 '{filename}' 不存在"

    range_err = _validate_line_range(lines, start_line, end_line)
    if range_err:
        return range_err

    if content and not content.endswith("\n"):
        content += "\n"

    new_lines = content.splitlines(keepends=True)
    lines[start_line - 1:end_line] = new_lines
    _write_lines(full_path, lines)

    old_count = end_line - start_line + 1
    new_count = len(new_lines)
    return f"已替换第 {start_line}~{end_line} 行（{old_count} 行 → {new_count} 行，文件现共 {len(lines)} 行）"


@tool(model=FindReplaceArgs, description="在文件中查找文本并替换", sensitive=True,
      confirm_template="在文件 '{filename}' 中将 '{old_text}' 替换为 '{new_text}'")
async def find_replace(filename: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    full_path, err = _safe_path(filename)
    if err:
        return err
    if not os.path.isfile(full_path):
        return f"错误：文件 '{filename}' 不存在"

    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()

    if old_text not in content:
        return f"未找到 '{old_text}'"

    if replace_all:
        count = content.count(old_text)
        new_content = content.replace(old_text, new_text)
    else:
        count = 1
        new_content = content.replace(old_text, new_text, 1)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return f"已替换 {count} 处"


def _format_size(size: int) -> str:
    """格式化文件大小"""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"
