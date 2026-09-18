"""静态环境基线采集测试。

基线作为首次 chat 前的外部上下文复用于各 agent，因此两条性质必须锁死：产出
**确定**（同一目录连调两次逐字节相同）以及产出**有界**（超大仓库不能撑爆输入）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.common.env_baseline import (
    _MAX_TOTAL_CHARS,
    _MAX_TREE_LINES,
    collect_env_baseline,
)


def test_reports_stack_markers_and_skips_ignored_dirs(tmp_path: Path) -> None:
    """技术栈入口被列出，构建产物与依赖目录被跳过。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "src" / "mgr").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".hidden").mkdir()

    text = collect_env_baseline(tmp_path)

    assert "pyproject.toml" in text
    assert "Makefile" in text
    assert "src" in text
    assert "mgr" in text  # 深度 2 的子目录内联在同一行
    assert "tests" in text
    assert "node_modules" not in text
    assert "__pycache__" not in text
    assert ".venv" not in text
    assert ".hidden" not in text


def test_output_is_deterministic(tmp_path: Path) -> None:
    """同一目录连续两次采集结果逐字节相同。

    这是缓存友好性的回归护栏：基线只要因调用而异，所有 agent 的 tools+system
    前缀缓存就会失效。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    for name in ("beta", "alpha", "gamma"):
        (tmp_path / name / "child").mkdir(parents=True)

    assert collect_env_baseline(tmp_path) == collect_env_baseline(tmp_path)


def test_accepts_str_workdir(tmp_path: Path) -> None:
    """workdir 传字符串时不报错（调用方并不总是传 Path）。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    assert collect_env_baseline(str(tmp_path))


def test_non_git_directory_degrades_gracefully(tmp_path: Path) -> None:
    """非 git 目录下不抛异常，并明确写出 git 不可用。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    text = collect_env_baseline(tmp_path)

    assert "git" in text
    assert "非 git 仓库或 git 不可用" in text


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="git", timeout=2.0),
        FileNotFoundError("git not installed"),
        OSError("boom"),
    ],
    ids=["timeout", "missing-git", "oserror"],
)
def test_git_failures_degrade_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """git 超时、缺失或其他 OS 错误一律降级，不阻塞启动。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。
        error: 模拟的子进程异常。

    Returns:
        None。
    """
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", _boom)

    text = collect_env_baseline(tmp_path)

    assert "非 git 仓库或 git 不可用" in text


def test_git_snapshot_reports_branch_and_short_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git 可用时分支名与短 hash 分别归位，不能两者都是分支名。

    `git rev-parse --abbrev-ref HEAD --short HEAD` 会让两行都输出分支名——
    `--abbrev-ref` 对其后所有参数生效。本用例锁死正确的参数顺序与截断。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。

    Returns:
        None。
    """
    captured: dict[str, object] = {}

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["args"] = args
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="7c854f76527638c439c8e2127e1cfa08b9ad207c\nfeature/x\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    text = collect_env_baseline(tmp_path)

    assert "分支 `feature/x`" in text
    assert "HEAD `7c854f7`" in text
    # 完整 hash 在前、--abbrev-ref 在后，否则两行都会是分支名
    assert captured["args"] == ["git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"]


def test_detached_head_is_labelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游离 HEAD 状态被显式标注，不伪装成名为 HEAD 的分支。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。

    Returns:
        None。
    """
    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="abcdef1234567890\nHEAD\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    text = collect_env_baseline(tmp_path)

    assert "detached HEAD `abcdef1`" in text
    assert "分支 `HEAD`" not in text


def test_large_repository_stays_within_budget(tmp_path: Path) -> None:
    """顶层目录极多时行数与字符数都不超预算，并提示有省略。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    for index in range(200):
        (tmp_path / f"module_{index:03d}" / "sub").mkdir(parents=True)

    text = collect_env_baseline(tmp_path)

    assert len(text) <= _MAX_TOTAL_CHARS
    tree_lines = [line for line in text.splitlines() if line.startswith("  ")]
    assert len(tree_lines) <= _MAX_TREE_LINES + 1  # +1 为省略提示行
    assert "未列出" in text


def test_empty_directory_still_returns_text(tmp_path: Path) -> None:
    """空目录不报错，至少给出 git 状态一行。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    text = collect_env_baseline(tmp_path)

    assert text
    assert "顶层结构" not in text  # 没有目录就不渲染树标题
