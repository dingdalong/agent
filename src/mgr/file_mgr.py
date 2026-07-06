from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import sys

if TYPE_CHECKING:
    from src.agent import AgentDeps


@lru_cache(maxsize=1)
def _resolve_rg() -> str | None:
    """定位 ripgrep 可执行文件：优先随 ripgrep 包安装到环境 bin 目录的二进制，回退到 PATH。

    Returns:
        rg 可执行文件的绝对路径；随包二进制与 PATH 均未命中时返回 None。
    """
    exe = "rg.exe" if sys.platform == "win32" else "rg"
    # 优先使用 ripgrep 包随 wheel 装入环境 bin 目录的二进制（不依赖主机预装 rg）
    try:
        dist = metadata.distribution("ripgrep")
        for f in dist.files or []:
            if f.name == exe:
                path = Path(dist.locate_file(f)).resolve()
                if path.exists():
                    return str(path)
    except metadata.PackageNotFoundError:
        pass
    # 回退到主机 PATH 中已安装的 rg
    return shutil.which(exe)

@dataclass
class FileMgr:
    workdir: Path
    deps: AgentDeps = field(repr=False)

    # 设计约定：本类所有公开方法均为「阻塞型」——内部做同步文件 I/O（read_text/
    # write_text/glob/rglob 等），因此一律声明为普通 def。卸载到线程由 @tool 装饰器
    # （decorator.py）统一处理：调用它们的工具包装也是普通 def，装饰器看到非协程即用
    # asyncio.to_thread 把整次调用丢进工作线程，事件循环全程不被阻塞。

    def safe_path(self, path_str: str) -> Path:
        """将路径字符串解析为绝对 Path。

        仅做路径解析，不做访问控制。
        工作区外路径的访问控制由 check_permissions 回调在权限层处理。

        Args:
            path_str: 文件或目录的路径字符串。

        Returns:
            解析后的绝对 Path 对象。
        """
        return Path(path_str).resolve()

    def _display_path(self, path: Path) -> str:
        """将路径转为显示用字符串：工作区内返回相对路径，工作区外返回绝对路径。

        Args:
            path: 要显示的路径。

        Returns:
            适合显示的路径字符串。
        """
        try:
            return str(path.relative_to(self.workdir))
        except ValueError:
            return str(path)

    def read_file(self, path: str,
                  start_line: int | None = None,
                  end_line: int | None = None) -> str:
        """读取文件内容并附带行号，可指定行范围。

        Args:
            path: 文件路径。
            start_line: 起始行号（从 1 开始）；None 表示从文件开头。
            end_line: 结束行号（包含该行）；None 表示读到文件末尾。

        Returns:
            带行号的文件内容文本，或错误描述字符串。
        """
        try:
            all_lines = self.safe_path(path).read_text().splitlines()
            total = len(all_lines)
            if start_line is None and end_line is None:
                selected_lines = all_lines
                first_line_no = 1
                range_info = ""
            else:
                start = start_line if start_line is not None else 1
                end = end_line if end_line is not None else total
                if start < 1 or end < 1 or start > end or end > total:
                    return f"Error: 行号范围无效 (文件共 {total} 行)"
                selected_lines = all_lines[start - 1:end]
                first_line_no = start
                range_info = f" | 行范围: {start}-{end}"

            rendered = self._render_numbered_lines(selected_lines, first_line_no)
            header = f"文件: {path} | 总行数: {total}{range_info} | 内容格式: 行号 | 内容"
            parts = [header]
            if rendered:
                parts.append(rendered)
            return "\n".join(parts)
        except Exception as exc:
            return f"Error: {exc}"

    def _render_numbered_lines(self, all_lines: list[str], first_line_no: int = 1) -> str:
        return "\n".join(
            f"{line_no:>4} | {line}"
            for line_no, line in enumerate(all_lines, first_line_no)
        )

    def write_file(self, path: str, content: str,
                   append: bool = False,
                   chunk_index: int | None = None,
                   total_chunks: int | None = None) -> str:
        """写入或追加文件内容，支持分块写入。

        Args:
            path: 文件路径。
            content: 要写入的内容。
            append: 是否追加写入。
            chunk_index: 分块写入时当前分块序号（从 1 开始）；None 表示非分块。
            total_chunks: 分块写入时总分块数；None 表示非分块。

        Returns:
            写入结果描述字符串，或错误描述字符串。
        """
        try:
            file_path = self.safe_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)

            is_chunked = chunk_index is not None and total_chunks is not None

            if append or (is_chunked and chunk_index > 1):
                with open(file_path, "a") as f:
                    f.write(content)
            else:
                file_path.write_text(content)

            written = len(content)
            total = len(file_path.read_text())

            if is_chunked:
                if chunk_index < total_chunks:
                    return (f"已写入分块 {chunk_index}/{total_chunks} "
                            f"({written} bytes), 文件当前大小: {total} bytes, "
                            f"等待下一分块...")
                else:
                    return (f"已写入最后分块 {chunk_index}/{total_chunks} "
                            f"({written} bytes), 文件写入完成: {path}, "
                            f"总大小: {total} bytes")

            mode = "追加" if append else "写入"
            return f"已{mode} {written} bytes 到 {path}, 文件总大小: {total} bytes"
        except Exception as exc:
            return f"Error: {exc}"

    def edit_file_lines(self, path: str, start_line: int,
                        new_text: str = "", end_line: int | None = None) -> str:
        """按行号编辑文件：替换、插入或删除。

        根据参数组合自动选择操作模式：
        - new_text 非空 + end_line → 替换 start_line 到 end_line 的内容
        - new_text 非空 + 无 end_line → 在 start_line 前插入
        - new_text 为空 + end_line → 删除 start_line 到 end_line

        Args:
            path: 文件绝对路径。
            start_line: 起始行号，从 1 开始。
            new_text: 新内容；空字符串表示删除模式。
            end_line: 结束行号（包含该行）；不传表示插入模式。

        Returns:
            操作结果描述字符串。
        """
        try:
            file_path = self.safe_path(path)
            lines = file_path.read_text().splitlines(keepends=True)
            total = len(lines)

            if not new_text and end_line is None:
                return "Error: new_text 和 end_line 不能同时为空"

            if end_line is None:
                # 插入模式
                if start_line < 1 or start_line > total + 1:
                    return f"Error: 行号无效 (文件共 {total} 行, 可插入范围 1-{total + 1})"
                insert = self._split_edit_lines(new_text)
                result_lines = lines[:start_line - 1] + insert + lines[start_line - 1:]
                file_path.write_text("".join(result_lines))
                return (f"已在第 {start_line} 行前插入 {len(insert)} 行 "
                        f"| 文件: {path} | 总行数: {len(result_lines)}")

            if start_line < 1 or end_line > total or start_line > end_line:
                return f"Error: 行号范围无效 (文件共 {total} 行)"

            if not new_text:
                # 删除模式
                result_lines = lines[:start_line - 1] + lines[end_line:]
                file_path.write_text("".join(result_lines))
                removed = end_line - start_line + 1
                return (f"已删除第 {start_line}-{end_line} 行 ({removed} 行) "
                        f"| 文件: {path} | 总行数: {len(result_lines)}")

            # 替换模式
            before = lines[:start_line - 1]
            after = lines[end_line:]
            insert = self._split_edit_lines(new_text)
            result_lines = before + insert + after
            file_path.write_text("".join(result_lines))
            removed = end_line - start_line + 1
            added = len(insert)
            return (f"已替换第 {start_line}-{end_line} 行 ({removed} 行 -> {added} 行) "
                    f"| 文件: {path} | 总行数: {len(result_lines)}")

        except Exception as exc:
            return f"Error: {exc}"

    def replace_all_in_file(self, path: str, old_text: str, new_text: str) -> str:
        """替换文件中所有匹配的文本。

        Args:
            path: 文件绝对路径。
            old_text: 要查找的原文本，必须非空。
            new_text: 替换后的新文本。

        Returns:
            操作结果描述字符串。
        """
        try:
            file_path = self.safe_path(path)
            content = file_path.read_text()
            if not old_text:
                return "Error: old_text 不能为空"
            found = content.count(old_text)
            if found == 0:
                total = len(content.splitlines())
                return f"Error: 未找到匹配文本 (文件共 {total} 行)"
            result = content.replace(old_text, new_text)
            file_path.write_text(result)
            return f"已替换 {found} 处匹配 | 文件: {path} | 总行数: {len(result.splitlines())}"

        except Exception as exc:
            return f"Error: {exc}"

    def _split_edit_lines(self, text: str) -> list[str]:
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        return lines

    def get_file_info(self, path: str) -> str:
        """获取文件或目录的元信息。

        Args:
            path: 文件或目录路径。

        Returns:
            元信息描述文本，或错误描述字符串。
        """
        try:
            file_path = self.safe_path(path)
            if not file_path.exists():
                return f"Error: 路径不存在: {path}"

            stat = file_path.stat()
            rel = self._display_path(file_path)
            kind = "目录" if file_path.is_dir() else "文件"
            created = datetime.fromtimestamp(stat.st_birthtime).strftime("%Y-%m-%d %H:%M:%S")
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            lines = [
                f"路径: {rel}",
                f"类型: {kind}",
            ]
            if file_path.is_file():
                lines.append(f"大小: {self._format_size(stat.st_size)} ({stat.st_size} bytes)")
                lines.append(f"扩展名: {file_path.suffix or '无'}")
                lines.append(f"行数: {len(file_path.read_text().splitlines())}")
            elif file_path.is_dir():
                children = list(file_path.iterdir())
                dirs = sum(1 for c in children if c.is_dir())
                files = len(children) - dirs
                lines.append(f"子项: {dirs} 个目录, {files} 个文件")
            lines.append(f"创建时间: {created}")
            lines.append(f"修改时间: {modified}")
            lines.append(f"权限: {oct(stat.st_mode)[-3:]}")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"

    def _format_size(self, size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"

    def _build_tree(self, dir_path: Path, prefix: str,
                    current_depth: int, max_depth: int) -> tuple[list[str], int, int]:
        lines: list[str] = []
        dir_count = 0
        file_count = 0
        try:
            entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            lines.append(f"{prefix}[权限不足]")
            return lines, 0, 0

        for i, entry in enumerate(entries):
            is_last = (i == len(entries) - 1)
            connector = "└── " if is_last else "├── "

            if entry.is_dir():
                dir_count += 1
                if current_depth < max_depth:
                    lines.append(f"{prefix}{connector}[DIR]  {entry.name}/")
                    child_prefix = prefix + ("    " if is_last else "│   ")
                    child_lines, cd, cf = self._build_tree(
                        entry, child_prefix, current_depth + 1, max_depth)
                    lines.extend(child_lines)
                    dir_count += cd
                    file_count += cf
                else:
                    lines.append(f"{prefix}{connector}[DIR]  {entry.name}/ (未展开)")
            else:
                file_count += 1
                size = self._format_size(entry.stat().st_size)
                lines.append(f"{prefix}{connector}[FILE] {entry.name} ({size})")

        return lines, dir_count, file_count

    def list_directory(self, path: str, max_depth: int = 3) -> str:
        """以树状结构列出目录内容。

        Args:
            path: 目录路径。
            max_depth: 递归展开的最大深度。

        Returns:
            目录树文本，或错误描述字符串。
        """
        try:
            dir_path = self.safe_path(path)
            if not dir_path.exists():
                return f"Error: 目录不存在: {path}"
            if not dir_path.is_dir():
                return f"Error: 不是目录: {path}"

            rel = self._display_path(dir_path)
            lines = [f"目录: {rel}/"]
            tree_lines, dir_count, file_count = self._build_tree(
                dir_path, "", 1, max_depth)
            lines.extend(tree_lines)
            lines.append(f"共 {dir_count} 个目录, {file_count} 个文件")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"

    def create_directory(self, path: str) -> str:
        """创建目录（含父目录）。

        Args:
            path: 要创建的目录路径。

        Returns:
            创建结果描述字符串。
        """
        try:
            dir_path = self.safe_path(path)
            if dir_path.exists():
                return f"目录已存在: {path}"
            dir_path.mkdir(parents=True, exist_ok=True)
            return f"已创建目录: {path}"
        except Exception as exc:
            return f"Error: {exc}"

    def move_file(self, source: str, destination: str) -> str:
        """移动或重命名文件/目录。

        Args:
            source: 源路径。
            destination: 目标路径；若为已存在目录，则移动到该目录下。

        Returns:
            移动结果描述字符串。
        """
        try:
            src_path = self.safe_path(source)
            dst_path = self.safe_path(destination)
            if not src_path.exists():
                return f"Error: 源路径不存在: {source}"
            if dst_path.is_dir():
                dst_path = dst_path / src_path.name
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.rename(dst_path)
            dst_rel = self._display_path(dst_path)
            kind = "目录" if dst_path.is_dir() else "文件"
            return f"已移动{kind}: {source} -> {dst_rel}"
        except Exception as exc:
            return f"Error: {exc}"

    def grep(self, pattern: str, path: str = ".") -> str:
        """用 ripgrep 按正则搜索文件内容，返回匹配的文件、行号与行文本。

        Args:
            pattern: ripgrep 正则表达式，默认区分大小写。
            path: 搜索起始路径（目录或文件）。

        Returns:
            每行为 "路径:行号:行文本" 的匹配结果，无命中或出错时返回相应提示。
        """
        try:
            search_root = self.safe_path(path)
            if not search_root.exists():
                return f"Error: 路径不存在: {path}"

            code, out, err = self._run_rg(
                ["--line-number", "--color=never", "--", pattern, str(search_root)]
            )
            if code == 1:
                return f'未找到匹配: "{pattern}"'
            if code >= 2:
                return f"Error: {err.strip() or 'rg 执行失败'}"

            lines = out.splitlines()
            max_lines = 200
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines.append("... 结果已截断，请缩小 path 或细化 pattern")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"

    def glob(self, pattern: str, path: str = ".") -> str:
        """用 ripgrep 按 glob 模式查找文件（遵守 .gitignore，排除隐藏文件，不含目录）。

        Args:
            pattern: 文件名/路径 glob，如 "*.py"、"**/config*.yaml"，默认递归匹配。
            path: 查找起始目录。

        Returns:
            匹配文件的相对路径列表，无命中或出错时返回相应提示。
        """
        try:
            search_root = self.safe_path(path)
            if not search_root.exists():
                return f"Error: 路径不存在: {path}"
            if not search_root.is_dir():
                return f"Error: 不是目录: {path}"

            code, out, err = self._run_rg(
                ["--files", "--color=never", "-g", pattern, str(search_root)]
            )
            if code >= 2:
                return f"Error: {err.strip() or 'rg 执行失败'}"

            rels = [self._display_path(Path(f)) for f in out.splitlines()]
            if not rels:
                return f"未找到匹配文件: {pattern}"
            header = f"找到 {len(rels)} 个文件:"
            max_lines = 200
            if len(rels) > max_lines:
                body = "\n".join(rels[:max_lines])
                return f"{header}\n{body}\n... 结果已截断（超过 {max_lines} 个），请缩小 path 或细化 pattern"
            return header + "\n" + "\n".join(rels)
        except Exception as exc:
            return f"Error: {exc}"

    def _run_rg(self, rg_args: list[str]) -> tuple[int, str, str]:
        """执行 ripgrep 子进程，使用随包安装的 rg（回退到 PATH 中的 rg）。

        Args:
            rg_args: 传给 rg 的参数列表（不含 "rg" 本身）。

        Returns:
            (returncode, stdout, stderr)；rg 缺失或超时时归为 returncode 2。
        """
        rg = _resolve_rg()
        if rg is None:
            return 2, "", "未找到 rg（ripgrep），请确认 ripgrep 依赖已安装"
        try:
            proc = subprocess.run(
                [rg, *rg_args], capture_output=True, text=True, timeout=30
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 2, "", "rg 执行超时"
