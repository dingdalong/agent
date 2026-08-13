"""工具包 — policy 元数据 + 全局工具注册表（@tool 装饰器）+ builtin 工具。

设计要点：builtin 工具的扫描是**延迟**的（PEP 562 __getattr__），仅在首次访问
ToolDict / ToolEntry / tool / _registry 时才执行。原因：policy 是独立的轻量子模块，
被 src.mgr.path_resolver 等基础模块引用；若 import policy 时就 eagerly 扫描 builtin，
而 builtin 工具又反向引用 src.mgr.*，就会在「先拉 src.mgr」的导入路径上形成
src.mgr/__init__ ↔ src.tools/__init__ 的包级循环导入。延迟扫描让 policy 保持轻量、
可被任意模块安全导入，从而切断该传递性循环。
"""

import importlib
import pkgutil

from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolOrigin, ToolPolicy

__all__ = [
    "AccessKind", "DataFlow", "PathArgument", "PathRole", "ToolDict", "ToolEntry",
    "ToolOrigin", "ToolPolicy", "tool", "_registry",
]

# 首次访问时触发一次 builtin 扫描并缓存 decorator 导出。
_scanned = False


def _scan_builtin() -> None:
    global _scanned
    if _scanned:
        return
    _scanned = True
    # 用 pkgutil 而非文件系统 glob 枚举：PyInstaller 冻结后 builtin/*.py 不以文件形式
    # 存在，glob 会静默返回空导致全部工具不注册；pkgutil 走 importer 协议，冻结与
    # 未冻结行为一致（PyInstaller 由 pyi_rth_pkgutil 运行时钩子提供支持）。
    from src.tools import builtin

    for info in sorted(pkgutil.iter_modules(builtin.__path__), key=lambda i: i.name):
        importlib.import_module(f".{info.name}", package=builtin.__name__)


def __getattr__(name: str):
    # 访问 decorator 提供的名字时，先确保 builtin 已扫描注册。
    if name in ("ToolDict", "ToolEntry", "tool", "_registry"):
        _scan_builtin()
        from src.tools import decorator
        return getattr(decorator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
