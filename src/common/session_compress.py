"""会话日志的冷压缩 — 把长期未更新的日志转成压缩归档。

压缩只发生在会话文件长时间没有新增批记录之后：先写临时文件、校验可完整解压回原文，
再用原子替换覆盖原日志，避免半个文件损坏已有历史。

编码器优先使用 zstandard（文件后缀 ``.jsonl.zst``）；环境未安装 zstandard 时回退到
标准库 lzma（``.jsonl.xz``），保证冷压缩不依赖外部包也能生效。
"""

from __future__ import annotations

import logging
import lzma
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)


COLD_LOG_AGE_SECONDS = 7 * 24 * 3600
MARKER_STALE_SECONDS = 6 * 3600
COMPRESSION_LEVEL = 3
MAX_CONCURRENT_JOBS = 2
MARKER_NAME = ".cold-compress.marker"
ZSTD_SUFFIX = ".jsonl.zst"
LZMA_SUFFIX = ".jsonl.xz"
COLD_SUFFIXES = (ZSTD_SUFFIX, LZMA_SUFFIX)


def _zstd_available() -> bool:
    """返回当前环境是否具备 zstd 压缩能力。"""
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


def decompress_log(path: Path) -> str:
    """解压一个冷日志文件。

    Args:
        path: ``.jsonl.zst`` 或 ``.jsonl.xz`` 文件路径。

    Returns:
        解压后的日志原文。

    Raises:
        OSError: 未知后缀、zstandard 缺失或解压失败。
    """
    if path.suffix == ".xz":
        return lzma.decompress(path.read_bytes()).decode("utf-8")
    try:
        import zstandard
    except ImportError as exc:
        raise OSError("缺少 zstandard，无法读取压缩会话日志") from exc
    with path.open("rb") as stream:
        with zstandard.ZstdDecompressor().stream_reader(stream) as reader:
            return reader.read().decode("utf-8")


def compress_log(path: Path, *, level: int = COMPRESSION_LEVEL) -> Path | None:
    """把一份日志压缩为同目录下的冷归档，成功后删除原文件。

    Args:
        path: 活跃日志文件路径。
        level: zstd 压缩级别。

    Returns:
        压缩文件路径；能力缺失或失败时返回 None。
    """
    use_zstd = _zstd_available()
    target_suffix = ZSTD_SUFFIX if use_zstd else LZMA_SUFFIX
    target = path.with_name(path.name[: -len(".jsonl")] + target_suffix)
    try:
        original = path.read_bytes()
    except OSError as exc:
        logger.warning("读取会话日志失败: %s", exc)
        return None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{target.name}.")
    os.close(fd)
    temp_path = Path(temporary)
    try:
        if use_zstd:
            import zstandard

            compressed = zstandard.ZstdCompressor(level=level).compress(original)
        else:
            compressed = lzma.compress(original, preset=6)
        with temp_path.open("wb") as stream:
            stream.write(compressed)
            stream.flush()
            os.fsync(stream.fileno())
        restored = _decompress_bytes(temp_path, use_zstd)
        if restored != original:
            raise ValueError("压缩校验失败")
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
        path.unlink(missing_ok=True)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except (OSError, ValueError) as exc:
        logger.warning("压缩会话日志失败: %s", exc)
        temp_path.unlink(missing_ok=True)
        return None
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    return target


def _decompress_bytes(path: Path, use_zstd: bool) -> bytes:
    """按编码器解压校验刚写出的归档。

    Args:
        path: 临时归档路径。
        use_zstd: 是否使用 zstandard 解码。

    Returns:
        解压后的原始字节。
    """
    if not use_zstd:
        return lzma.decompress(path.read_bytes())
    import zstandard

    with path.open("rb") as stream:
        return zstandard.ZstdDecompressor().stream_reader(stream).read()


def cold_candidates(
    sessions_dir: Path,
    *,
    now: float | None = None,
    age_seconds: int = COLD_LOG_AGE_SECONDS,
) -> list[Path]:
    """列出已达到冷压缩年龄的活跃日志。

    Args:
        sessions_dir: 会话目录。
        now: 当前时间戳，默认取系统时间。
        age_seconds: 距离最后一次修改的最小秒数。

    Returns:
        待压缩日志路径列表，按修改时间升序。
    """
    if not sessions_dir.is_dir():
        return []
    deadline = (time.time() if now is None else now) - age_seconds
    candidates: list[tuple[float, Path]] = []
    for path in sessions_dir.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime <= deadline:
            candidates.append((mtime, path))
    candidates.sort()
    return [path for _mtime, path in candidates]


def run_cold_compression(
    sessions_dir: Path,
    *,
    now: float | None = None,
    max_jobs: int = MAX_CONCURRENT_JOBS,
) -> int:
    """压缩所有够冷的会话日志并返回处理数量。

    Args:
        sessions_dir: 会话目录。
        now: 当前时间戳，默认取系统时间。
        max_jobs: 并发压缩任务上限。

    Returns:
        成功压缩的日志数量；能力缺失时为 0。
    """
    marker = sessions_dir / MARKER_NAME
    current = time.time() if now is None else now
    try:
        if marker.exists() and current - marker.stat().st_mtime < MARKER_STALE_SECONDS:
            return 0
    except OSError:
        return 0
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(current), encoding="utf-8")
    except OSError:
        return 0
    candidates = cold_candidates(sessions_dir, now=current)
    if not candidates:
        return 0
    count = 0
    with ThreadPoolExecutor(max_workers=max(1, max_jobs)) as pool:
        for result in pool.map(compress_log, candidates):
            if result is not None:
                count += 1
    return count
