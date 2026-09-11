"""有界文本读取；授权与执行共用路径凭据。"""
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from pathlib import Path
import shutil
import sys
from typing import Any

from src.mgr.frozen import bundled_path
from src.mgr.path_resolver import MAX_READ_FILE_BYTES, PathResolver
from src.tools.display import FileContent, ToolResult


@lru_cache(maxsize=1)
def _resolve_rg() -> str | None:
    """定位 ripgrep 可执行文件：优先冻结产物内置，其次随包安装到环境 bin 目录，最后 PATH。

    Returns:
        rg 可执行文件的绝对路径；三条来源均未命中时返回 None。
    """
    exe = "rg.exe" if sys.platform == "win32" else "rg"
    # 冻结产物里 dist-info 与 bin/ 布局都不存在，只能按打包时的落点直接找
    bundled = bundled_path(exe)
    if bundled is not None:
        return str(bundled)
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
    deps: Any = field(repr=False)
    _path_resolver: PathResolver = field(init=False, repr=False)

    def __post_init__(self):
        self._path_resolver = PathResolver(self.workdir)

    def read_file(self, path, authorization, *, offset=1, column=0, limit=None):
        if offset < 1 or column < 0 or (limit is not None and limit < 1):
            return ToolResult.failure('invalid_arguments', 'offset/limit 必须为正整数，column 不能为负数')
        try:
            if not authorization.allowed:
                return ToolResult.failure('permission_denied', '缺少读取授权')
            grant = next(g for g in authorization.path_grants if g.argument == 'path')
            target = self._path_resolver.revalidate(grant, path)
            self._path_resolver.validate_local_read(target)
            # 脱敏先于范围选择，跨行秘密也不会因切片而漏过检测。
            with target.open('rb') as stream:
                data = stream.read(MAX_READ_FILE_BYTES + 1)
            if len(data) > MAX_READ_FILE_BYTES:
                return ToolResult.failure('read_failed', '单文件超过 8 MiB')
            text = data.decode('utf-8')
            text = self.deps.data_guard.redact_source(text)
            lines = text.splitlines(keepends=True)
            if column and (offset > len(lines) or column >= len(lines[offset - 1])):
                return ToolResult.failure('invalid_arguments', 'column 超出起始行；读取当前文件时请重新定位')
            selected = lines[offset - 1:None if limit is None else offset - 1 + limit]
            content = FileContent(str(self.deps.data_guard.redact(str(target))), selected, len(lines), offset, column)
            body = ''.join(selected)[column:]
            return ToolResult(body, file_content=content)
        except (OSError, UnicodeError, ValueError, StopIteration) as exc:
            return ToolResult.failure('read_failed', str(exc))
