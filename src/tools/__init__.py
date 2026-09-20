"""工具包 — 全局工具注册表（@tool 装饰器）+ builtin 工具扫描。

工具策略与展示词汇不在这里：声明式授权策略（`ToolPolicy`/`ToolAvailability` 等）位于
`src/common/tool_policy.py`，展示结构（`ToolResult`/`ToolDisplay` 等）位于
`src/common/tool_display.py`，因为 interfaces、mgr、common 都直接复用它们。

builtin 工具在**包导入时**扫描注册。`src/tools/**` 运行期不依赖 `src.mgr`/`src.agent`/
`src.app`（由 tests/test_module_layout.py 的层次不变式守护），所以扫描不会形成包级循环
导入，也无须把注册推迟到首次属性访问。

枚举一律用 pkgutil 而非文件系统 glob：PyInstaller 冻结后 `builtin/*.py` 不以文件形式
存在，glob 会静默返回空导致全部工具不注册；pkgutil 走 importer 协议，冻结与未冻结行为
一致（PyInstaller 由 pyi_rth_pkgutil 运行时钩子提供支持）。
"""

import importlib
import pkgutil

from src.tools.decorator import ToolDict, ToolEntry, _registry, tool

__all__ = ["ToolDict", "ToolEntry", "_registry", "tool"]


def _scan_builtin() -> None:
    """按包内模块枚举注册全部内置工具（幂等，仅在导入本包时执行一次）。"""
    from src.tools import builtin

    for info in sorted(pkgutil.iter_modules(builtin.__path__), key=lambda i: i.name):
        importlib.import_module(f".{info.name}", package=builtin.__name__)


_scan_builtin()
