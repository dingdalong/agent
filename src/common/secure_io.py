"""Owner-only 原子文件写入与追加。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        # os.fchmod 仅 POSIX 提供，Windows 上无此属性（AttributeError），需守护
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding=encoding) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def append_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """以 owner-only 权限把一段文本追加到文件末尾。

    Args:
        path: 目标文件路径，父目录不存在时按 0o700 创建。
        content: 追加的完整文本；调用方负责包含换行。
        encoding: 文件编码。

    Returns:
        None。
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding=encoding) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
