"""tests/packaging 共用的构建产物定位。

放在 conftest 而非普通模块里：`AGENT_REQUIRE_BUILD=1` 的"产物缺失即失败"守卫需要
在整个目录的收集期生效，conftest 无论 `-k` 如何过滤都会被导入，普通模块只有在
导入它的测试模块被收集时才会触发。
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
DIST = ROOT / "dist"
# 与 scripts/build_exe.py 的 platform_tag() 保持一致
SYSTEM = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
MACHINE = {"amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
    platform.machine().lower(), platform.machine().lower()
)
EXE = "agent.exe" if platform.system() == "Windows" else "agent"
INSTALLER = "install.bat" if platform.system() == "Windows" else "install.sh"


def _find_payload() -> Path | None:
    """定位当前平台的构建产物目录（顶层含入口可执行文件与 _internal/）。"""
    for candidate in sorted(DIST.glob(f"agent-*-{SYSTEM}-{MACHINE}")):
        if (candidate / EXE).is_file():
            return candidate
    return None


PAYLOAD = _find_payload()
BINARY = PAYLOAD / EXE if PAYLOAD is not None else None
# CI 里构建失败时产物不存在，若仍按 skip 处理会让整条流水线显示为绿色。
# 设 AGENT_REQUIRE_BUILD=1 把「找不到产物」从跳过升级为失败。
if PAYLOAD is None and os.environ.get("AGENT_REQUIRE_BUILD") == "1":
    raise RuntimeError(f"AGENT_REQUIRE_BUILD=1 但未找到 {SYSTEM}-{MACHINE} 构建产物（{DIST}）")
requires_build = pytest.mark.skipif(
    PAYLOAD is None,
    reason=f"未找到 {SYSTEM}-{MACHINE} 构建产物，请先运行 make build",
)
