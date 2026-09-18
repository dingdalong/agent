"""冻结产物运行时 — 集中处理 PyInstaller 打包后与源码运行的差异。

只在被 PyInstaller 冻结时生效；源码运行下所有函数都退化为无副作用的默认行为，
调用方因此无需自己判断运行形态。

本模块是叶子模块：只依赖标准库，可被任意层安全导入。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

# 冻结产物里可能被改写以指向包内目录的搜索路径变量。两种改写方式要分别处理：
# - Linux/Unix 的 LD_LIBRARY_PATH 由引导器改写，原值存进 <名字>_ORIG，可整体还原；
# - macOS 的 DYLD_* 与 Windows 的 PATH 引导器不碰，但个别包的运行时钩子会往里塞
#   _MEIPASS 路径，没有 _ORIG 可还原，只能逐条剔除锚定在包内的那些。
# 不在此列的变量（如 SSL_CERT_FILE）不受这套机制影响，动它们会误删用户自己的设置。
_SEARCH_PATH_VARS = (
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "LIBPATH",
    "PATH",
)


def _strip_bundle_entries(value: str, root: Path) -> str:
    """从 PATH 型变量里剔除锚定在冻结产物内的条目。

    Args:
        value: 原始变量值（os.pathsep 分隔）。
        root: 冻结产物资源根目录。

    Returns:
        剔除后的变量值；全部条目都在包内时返回空串。
    """
    kept = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).resolve().is_relative_to(root):
                continue
        except OSError:  # pragma: no cover - 非法路径按保留处理
            pass
        kept.append(entry)
    return os.pathsep.join(kept)


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 冻结产物中。

    Returns:
        冻结产物中为 True，源码运行为 False。
    """
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path | None:
    """返回冻结产物的资源根目录（onedir 下即 _internal/）。

    Returns:
        冻结时为 sys._MEIPASS 对应目录；源码运行返回 None。
    """
    return Path(sys._MEIPASS) if is_frozen() else None


def bundled_path(*parts: str) -> Path | None:
    """定位随包分发的资源，仅在冻结产物中存在时返回。

    Args:
        *parts: 相对 bundle_root() 的路径片段。

    Returns:
        存在的绝对路径；非冻结或文件缺失时返回 None，由调用方走各自的回退链。
    """
    root = bundle_root()
    if root is None:
        return None
    path = root.joinpath(*parts)
    return path if path.exists() else None


def clean_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回抹去 PyInstaller 注入痕迹的环境变量副本，供子进程使用。

    冻结产物的动态库搜索路径被指向了包内目录，子进程（MCP server、用户 shell 命令、
    hook 脚本）继承后会加载错动态库——其中 MCP server 常常本身就是另一个 Python
    程序，后果最严重。

    只改 _SEARCH_PATH_VARS 里的变量，且优先按 <名字>_ORIG 还原、没有 _ORIG 时只剔除
    锚定在包内的条目，不整体删除——避免误删用户自己设的值。

    源码运行时只是原样复制，调用方因此无需自己判断运行形态。

    Args:
        base: 作为基底的环境变量映射；None 时用 os.environ。

    Returns:
        可直接传给 subprocess / asyncio 的环境变量字典。
    """
    env = dict(os.environ if base is None else base)
    root = bundle_root()
    if root is None:
        return env

    for name in _SEARCH_PATH_VARS:
        original = env.pop(f"{name}_ORIG", None)
        if original is not None:
            env[name] = original
            continue
        current = env.get(name)
        if not current:
            continue
        stripped = _strip_bundle_entries(current, root)
        if stripped:
            env[name] = stripped
        else:
            env.pop(name, None)
    return env


def setup_tiktoken_cache() -> None:
    """把 tiktoken 指向随包预热的 BPE 缓存，实现首次启动零联网。

    tiktoken 在缓存未命中时会联网下载编码文件；构建期已把所需编码预热进
    _internal/tiktoken_cache/，这里在任何 tiktoken 调用之前指过去。
    已显式设置 TIKTOKEN_CACHE_DIR 的用户优先，不覆盖。
    """
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        return
    cache_dir = bundled_path("tiktoken_cache")
    if cache_dir is not None:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
