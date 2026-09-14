"""定位运行时使用的 ripgrep 可执行文件。"""

from functools import lru_cache
from importlib import metadata
from pathlib import Path
import shutil
import sys

from src.mgr.frozen import bundled_path


@lru_cache(maxsize=1)
def resolve_rg() -> str | None:
    """优先使用冻结产物或 Python 环境随包安装的 rg，最后查询 PATH。"""
    exe = "rg.exe" if sys.platform == "win32" else "rg"
    bundled = bundled_path(exe)
    if bundled is not None:
        return str(bundled)
    try:
        dist = metadata.distribution("ripgrep")
        for item in dist.files or []:
            if item.name == exe:
                path = Path(dist.locate_file(item)).resolve()
                if path.exists():
                    return str(path)
    except metadata.PackageNotFoundError:
        pass
    return shutil.which(exe)
