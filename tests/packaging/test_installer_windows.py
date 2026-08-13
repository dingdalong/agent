"""install.bat 的静态形态检查与 Windows 行为检查。

静态检查在所有平台运行：bat 有两类静默失效在 macOS/Linux 上根本没法靠执行发现，
但完全可以靠读文件发现，所以把它们钉成测试：

  1. 首行 BOM —— cmd 会把 BOM 连同 @echo off 一起当命令，报「不是内部或外部命令」。
  2. `if COND a & b` —— b 会无条件执行。写成 `if COND ( a & b )` 才是条件执行。
  3. goto/call 指向不存在的标签 —— cmd 报「系统找不到指定的批标签」并中止。

行为检查只能在 Windows 上跑，且绝不碰真实注册表：安装用 --skip-path 短路 PATH 写入，
PATH 合并逻辑改用 --print-path-merge 干跑断言。
"""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import pytest

from .conftest import PAYLOAD, ROOT, requires_build

INSTALL_BAT = ROOT / "scripts" / "install.bat"
windows_only = pytest.mark.skipif(
    platform.system() != "Windows", reason="install.bat 只能在 Windows 上执行"
)

# 命令分隔用的裸 &：排除 && 、重定向的 >& 、以及 echo 里转义的 ^&
BARE_AMP = re.compile(r"(?<![&>^])&(?!&)")


def bat_lines() -> list[str]:
    """按行读取 install.bat（源码形态，不解码行尾）。"""
    return INSTALL_BAT.read_bytes().decode("utf-8").splitlines()


def code_lines() -> list[str]:
    """install.bat 里参与执行的行，剔除 rem 与 :: 注释。"""
    return [
        line
        for line in bat_lines()
        if not line.strip().lower().startswith(("rem ", "::"))
    ]


def test_bat_has_no_bom_and_crlf() -> None:
    """UTF-8 无 BOM + CRLF，见 .editorconfig 的 [*.bat] 段。"""
    data = INSTALL_BAT.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "install.bat 带了 BOM，cmd 会让首行失效"
    assert data.count(b"\n") == data.count(b"\r\n"), "install.bat 含裸 LF 行"


def test_bat_conditional_commands_are_grouped() -> None:
    """`if COND a & b` 里的 b 无条件执行 —— 多条命令必须用括号分组。

    这个错误在 bat 里没有任何提示：命令照跑，只是分支语义悄悄错了。
    """
    offenders = []
    for number, line in enumerate(bat_lines(), start=1):
        stripped = line.strip()
        if not stripped.lower().startswith(("if ", "for ")):
            continue
        if "(" in stripped:
            continue
        if BARE_AMP.search(stripped):
            offenders.append(f"{number}: {stripped}")
    assert not offenders, "以下行的 & 之后会无条件执行，请加括号：\n" + "\n".join(offenders)


def test_bat_goto_targets_exist() -> None:
    """goto / call 的标签都得真实存在，否则 cmd 直接中止。"""
    lines = bat_lines()
    labels = {m.group(1).lower() for m in (re.match(r"^:(\w+)", ln) for ln in lines) if m}
    missing = []
    for number, line in enumerate(lines, start=1):
        for match in re.finditer(r"(?:goto|call)\s+:(\w+)", line, re.IGNORECASE):
            target = match.group(1).lower()
            if target != "eof" and target not in labels:
                missing.append(f"{number}: :{target}")
    assert not missing, "以下标签不存在：\n" + "\n".join(missing)


def test_bat_never_uses_setx() -> None:
    """setx 超过 1024 字符静默截断，且 %PATH% 是 Machine+User 合并值。

    用它写用户 PATH 会损坏用户环境，一律走 reg.exe。
    """
    hits = [line for line in code_lines() if re.search(r"\bsetx\b", line, re.IGNORECASE)]
    assert not hits, "install.bat 用了 setx：\n" + "\n".join(hits)


@windows_only
@requires_build
def test_shim_forwards_to_versioned_exe(tmp_path: Path) -> None:
    """转发脚本能把参数带到版本目录里的 agent.exe。"""
    assert PAYLOAD is not None
    env = {**__import__("os").environ, "LOCALAPPDATA": str(tmp_path)}
    proc = subprocess.run(
        ["cmd", "/c", str(INSTALL_BAT), "--from", str(PAYLOAD), "--skip-path", "--no-pause"],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    shim = tmp_path / "Programs/agent/bin/agent.bat"
    assert shim.is_file()
    # cmd 读到 BOM 会让转发脚本首行失效
    assert not shim.read_bytes().startswith(b"\xef\xbb\xbf")

    version = (PAYLOAD / "VERSION").read_text(encoding="utf-8").strip()
    assert (tmp_path / "Programs/agent" / version / "agent.exe").is_file()

    proc = subprocess.run(
        ["cmd", "/c", str(shim), "--self-check"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(tmp_path),
    )
    assert '"ok": true' in proc.stdout.lower(), proc.stdout[:2000]


def run_merge(raw: str, add: str) -> subprocess.CompletedProcess[str]:
    """干跑 PATH 合并逻辑，不落盘。"""
    return subprocess.run(
        ["cmd", "/c", str(INSTALL_BAT), "--print-path-merge", raw, add],
        capture_output=True,
        text=True,
        timeout=60,
    )


@windows_only
def test_merge_path_appends_when_absent() -> None:
    """缺失时追加到末尾。"""
    got = run_merge(r"C:\a;C:\b", r"C:\new")
    assert got.returncode == 0
    assert got.stdout.strip() == r"C:\a;C:\b;C:\new"


@windows_only
def test_merge_path_is_case_insensitive_noop() -> None:
    """已存在（忽略大小写）时退出码 2 且不产出新值 —— 重复安装不该把 PATH 越写越长。"""
    got = run_merge(r"C:\a;c:\NEW;C:\b", r"C:\new")
    assert got.returncode == 2
    assert got.stdout.strip() == ""


@windows_only
def test_merge_path_matches_whole_entry_only() -> None:
    """C:\\new 不该被 C:\\newer 误判为已存在。"""
    got = run_merge(r"C:\a;C:\newer", r"C:\new")
    assert got.returncode == 0
    assert got.stdout.strip().endswith(r";C:\new")


@windows_only
def test_merge_path_preserves_unexpanded_vars() -> None:
    """原值里的 %VAR% 必须逐字保留，不能被展开成字面量绝对路径。

    用一个不存在的变量名，避免 cmd 在传参时就把它展开掉。
    """
    raw = r"%AGENT_NOT_A_REAL_VAR%\bin;C:\a"
    got = run_merge(raw, r"C:\new")
    assert got.returncode == 0
    assert got.stdout.strip() == raw + r";C:\new"


@windows_only
def test_merge_path_refuses_overlong_value() -> None:
    """超长时拒绝写入（退出码 3），绝不产出被截断的值。"""
    got = run_merge(";".join([r"C:\padpadpadpad"] * 200), r"C:\new")
    assert got.returncode == 3
    assert got.stdout.strip() == ""
