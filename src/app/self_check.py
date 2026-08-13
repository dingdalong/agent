"""自检 — 在进程内核对那些「冻结后会静默降级」的能力。

打包引入的失效大多不会报错：工具扫描落空、内置命令消失、随包资源找不到、
编码文件缺失后偷偷联网。这些在源码运行时全都正常，只有在冻结产物里才暴露，
且表现为「能启动但不干活」。本模块把它们变成可断言的显式结果。

以 `agent --self-check` 运行，输出 JSON 供 tests/packaging 下的冒烟测试解析。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# 与 src/tools/builtin 下 @tool 装饰器的实际数量对齐；新增内置工具时同步更新。
EXPECTED_TOOL_COUNT = 32
EXPECTED_COMMANDS = {"plan", "clear", "resume", "agents", "models", "help"}
EXPECTED_ROLES = {"coding", "mijia", "onboard"}
# anthropic provider 固定用 cl100k_base，openai provider 未收录模型名回退 o200k_base
REQUIRED_ENCODINGS = ("o200k_base", "cl100k_base")


def _check_tools() -> dict[str, Any]:
    """内置工具是否全部注册（验证 pkgutil 枚举在冻结产物里确实生效）。"""
    from src.tools import _registry

    count = len(_registry)
    return {
        "ok": count == EXPECTED_TOOL_COUNT,
        "count": count,
        "expected": EXPECTED_TOOL_COUNT,
    }


def _check_commands(workdir: Path) -> dict[str, Any]:
    """内置 slash 命令是否全部加载。"""
    from src.commands import CommandMgr

    mgr = CommandMgr(workdir=workdir, global_dir=None, project_trusted=False)
    names = {entry.name for entry in mgr._commands.values()}
    missing = sorted(EXPECTED_COMMANDS - names)
    return {"ok": not missing, "missing": missing, "count": len(names)}


def _check_resources() -> dict[str, Any]:
    """builtin_root() 相对定位的随包资源是否都在。"""
    from src.mgr.paths import builtin_root

    root = builtin_root()
    targets = {
        "config.yaml": root / "config.yaml",
        "agent.tcss": root / "interfaces" / "tui" / "agent.tcss",
        "deepseek_tokenizer": root / "llm" / "tokenizer" / "deepseek" / "tokenizer.json",
    }
    missing = sorted(name for name, path in targets.items() if not path.is_file())
    roles_dir = root / "roles"
    roles = (
        {p.name for p in roles_dir.iterdir() if (p / "role.md").is_file()}
        if roles_dir.is_dir()
        else set()
    )
    missing_roles = sorted(EXPECTED_ROLES - roles)
    return {
        "ok": not missing and not missing_roles,
        "builtin_root": str(root),
        "missing": missing,
        "roles": sorted(roles),
        "missing_roles": missing_roles,
    }


def _check_ripgrep() -> dict[str, Any]:
    """rg 是否定位得到；冻结产物应命中随包副本而非宿主 PATH。"""
    import os

    from src.mgr.file_mgr import _resolve_rg
    from src.mgr.frozen import bundle_root

    path = _resolve_rg()
    root = bundle_root()
    bundled = bool(path and root and Path(path).is_relative_to(root))
    return {
        "ok": bool(path) and os.access(path, os.X_OK),
        "path": path,
        "bundled": bundled,
    }


def _check_deepseek_tokenizer() -> dict[str, Any]:
    """DeepSeek tokenizer 能否从随包 tokenizer.json 加载并计数。"""
    from src.llm.deepseek import DeepSeekProvider

    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    tokenizer = DeepSeekProvider._tokenizer.func(provider)
    count = len(tokenizer.encode("你好，世界！hello world").ids)
    return {"ok": count > 0, "tokens": count}


def _check_tiktoken() -> dict[str, Any]:
    """tiktoken 编码能否离线取到（编码构造器发现 + 预热缓存命中）。"""
    import tiktoken

    results: dict[str, Any] = {}
    for name in REQUIRED_ENCODINGS:
        try:
            results[name] = len(tiktoken.get_encoding(name).encode("hello"))
        except Exception as exc:
            results[name] = f"ERROR: {exc}"
    ok = all(isinstance(v, int) and v > 0 for v in results.values())
    return {"ok": ok, "encodings": results, "available": len(tiktoken.list_encoding_names())}


def run_self_check() -> int:
    """执行全部自检项并把结果以 JSON 打到 stdout。

    Returns:
        全部通过返回 0，否则返回 1。
    """
    from src.mgr.frozen import is_frozen

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        checks: dict[str, Any] = {
            "tools": _check_tools(),
            "commands": _check_commands(workdir),
            "resources": _check_resources(),
            "ripgrep": _check_ripgrep(),
            "deepseek_tokenizer": _check_deepseek_tokenizer(),
            "tiktoken": _check_tiktoken(),
        }

    failed = sorted(name for name, result in checks.items() if not result["ok"])
    report = {"frozen": is_frozen(), "ok": not failed, "failed": failed, "checks": checks}
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if not failed else 1
