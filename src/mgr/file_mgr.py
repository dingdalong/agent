from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

if TYPE_CHECKING:
    from src.agent import AgentDeps

@dataclass
class FileMgr:
    workdir: Path
    deps: AgentDeps = field(repr=False)

    def safe_path(self, path_str: str) -> Path:
        path = (self.workdir / path_str).resolve()
        if not path.is_relative_to(self.workdir):
            raise ValueError(f"Path escapes workspace: {path_str}")
        return path

    async def read_file(self, path: str, page: int = 1) -> str:
        try:
            all_lines = self.safe_path(path).read_text().splitlines()
            total = len(all_lines)
            rendered = self._render_numbered_lines(all_lines)
            pages = self.deps.llm.split_page(rendered)
            total_pages = len(pages)
            page = max(1, min(page, total_pages))

            selected = pages[page - 1]

            header = f"文件: {path} | 总行数: {total} | 第 {page}/{total_pages} 页"

            parts = [header]
            if selected:
                parts.append(selected)
            if page < total_pages:
                parts.append(f"\n(还有 {total_pages - page} 页，传入 page={page + 1} 继续读取)")

            return "\n".join(parts)
        except Exception as exc:
            return f"Error: {exc}"

    def _render_numbered_lines(self, all_lines: list[str]) -> str:
        return "\n".join(
            f"{line_no:>4} | {line}"
            for line_no, line in enumerate(all_lines, 1)
        )

    async def write_file(self, path: str, content: str,
                         append: bool = False,
                         chunk_index: int | None = None,
                         total_chunks: int | None = None) -> str:
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

    async def edit_file(self, path: str,
                        mode: str = "replace",
                        old_text: str | None = None,
                        new_text: str | None = None,
                        start_line: int | None = None,
                        end_line: int | None = None,
                        count: int = 1) -> str:
        try:
            file_path = self.safe_path(path)
            content = file_path.read_text()
            lines = content.splitlines(keepends=True)
            total = len(lines)

            if mode == "replace":
                if not old_text:
                    return "Error: replace 模式需要 old_text"
                found = content.count(old_text)
                if found == 0:
                    return f"Error: 未找到匹配文本 (文件共 {total} 行)"
                replaced = count if count > 0 else found
                result = content.replace(old_text, new_text or "", replaced)
                file_path.write_text(result)
                actual = min(replaced, found)
                return f"已替换 {actual} 处匹配 | 文件: {path} | 总行数: {len(result.splitlines())}"

            elif mode == "range_replace":
                if start_line is None or end_line is None:
                    return "Error: range_replace 模式需要 start_line 和 end_line"
                if start_line < 1 or end_line > total or start_line > end_line:
                    return f"Error: 行号范围无效 (文件共 {total} 行)"
                before = lines[:start_line - 1]
                after = lines[end_line:]
                insert = (new_text or "").splitlines(keepends=True)
                if insert and not insert[-1].endswith("\n"):
                    insert[-1] += "\n"
                result_lines = before + insert + after
                file_path.write_text("".join(result_lines))
                removed = end_line - start_line + 1
                added = len(insert)
                return (f"已替换第 {start_line}-{end_line} 行 ({removed} 行 -> {added} 行) "
                        f"| 文件: {path} | 总行数: {len(result_lines)}")

            elif mode == "insert":
                if start_line is None:
                    return "Error: insert 模式需要 start_line"
                if start_line < 1 or start_line > total + 1:
                    return f"Error: 行号无效 (文件共 {total} 行, 可插入范围 1-{total + 1})"
                insert = (new_text or "").splitlines(keepends=True)
                if insert and not insert[-1].endswith("\n"):
                    insert[-1] += "\n"
                result_lines = lines[:start_line - 1] + insert + lines[start_line - 1:]
                file_path.write_text("".join(result_lines))
                return (f"已在第 {start_line} 行前插入 {len(insert)} 行 "
                        f"| 文件: {path} | 总行数: {len(result_lines)}")

            elif mode == "delete":
                if start_line is None or end_line is None:
                    return "Error: delete 模式需要 start_line 和 end_line"
                if start_line < 1 or end_line > total or start_line > end_line:
                    return f"Error: 行号范围无效 (文件共 {total} 行)"
                result_lines = lines[:start_line - 1] + lines[end_line:]
                file_path.write_text("".join(result_lines))
                removed = end_line - start_line + 1
                return (f"已删除第 {start_line}-{end_line} 行 ({removed} 行) "
                        f"| 文件: {path} | 总行数: {len(result_lines)}")

            else:
                return f"Error: 未知模式 '{mode}', 支持: replace, range_replace, insert, delete"

        except Exception as exc:
            return f"Error: {exc}"

    async def get_file_info(self, path: str) -> str:
        try:
            file_path = self.safe_path(path)
            if not file_path.exists():
                return f"Error: 路径不存在: {path}"

            stat = file_path.stat()
            rel = file_path.relative_to(self.workdir)
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

    def _build_tree(self, dir_path: Path, prefix: str, recursive: bool,
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
                if recursive and current_depth < max_depth:
                    lines.append(f"{prefix}{connector}[DIR]  {entry.name}/")
                    child_prefix = prefix + ("    " if is_last else "│   ")
                    child_lines, cd, cf = self._build_tree(
                        entry, child_prefix, True, current_depth + 1, max_depth)
                    lines.extend(child_lines)
                    dir_count += cd
                    file_count += cf
                else:
                    suffix = " (未展开)" if recursive else ""
                    lines.append(f"{prefix}{connector}[DIR]  {entry.name}/{suffix}")
            else:
                file_count += 1
                size = self._format_size(entry.stat().st_size)
                lines.append(f"{prefix}{connector}[FILE] {entry.name} ({size})")

        return lines, dir_count, file_count

    async def list_directory(self, path: str,
                             recursive: bool = False, max_depth: int = 3) -> str:
        try:
            dir_path = self.safe_path(path)
            if not dir_path.exists():
                return f"Error: 目录不存在: {path}"
            if not dir_path.is_dir():
                return f"Error: 不是目录: {path}"

            rel = dir_path.relative_to(self.workdir)
            lines = [f"目录: {rel}/"]
            tree_lines, dir_count, file_count = self._build_tree(
                dir_path, "", recursive, 1, max_depth)
            lines.extend(tree_lines)
            lines.append(f"共 {dir_count} 个目录, {file_count} 个文件")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"

    async def create_directory(self, path: str) -> str:
        try:
            dir_path = self.safe_path(path)
            if dir_path.exists():
                return f"目录已存在: {path}"
            dir_path.mkdir(parents=True, exist_ok=True)
            return f"已创建目录: {path}"
        except Exception as exc:
            return f"Error: {exc}"

    async def move_file(self, source: str, destination: str) -> str:
        try:
            src_path = self.safe_path(source)
            dst_path = self.safe_path(destination)
            if not src_path.exists():
                return f"Error: 源路径不存在: {source}"
            if dst_path.is_dir():
                dst_path = dst_path / src_path.name
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.rename(dst_path)
            dst_rel = dst_path.relative_to(self.workdir)
            kind = "目录" if dst_path.is_dir() else "文件"
            return f"已移动{kind}: {source} -> {dst_rel}"
        except Exception as exc:
            return f"Error: {exc}"

    async def find_files(self, pattern: str, path: str = ".") -> str:
        try:
            search_root = self.safe_path(path)
            if not search_root.exists():
                return f"Error: 路径不存在: {path}"
            if not search_root.is_dir():
                return f"Error: 不是目录: {path}"

            matches = sorted(search_root.glob(pattern))
            matches = [m for m in matches if m.is_relative_to(self.workdir)]

            rel_root = search_root.relative_to(self.workdir)
            lines = [
                f"匹配模式: {pattern}",
                f"搜索路径: {rel_root}/",
                f"找到 {len(matches)} 个文件:",
            ]
            for m in matches:
                rel = m.relative_to(self.workdir)
                if m.is_dir():
                    lines.append(f"  [DIR]  {rel}/")
                else:
                    size = self._format_size(m.stat().st_size)
                    lines.append(f"  [FILE] {rel} ({size})")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"
