"""验证 list_directory 默认隐藏隐藏文件 + include_hidden 开关。"""

from __future__ import annotations

from pathlib import Path

import src.app.bootstrap  # noqa: F401  先就绪 src.tools，避免循环 import
from src.mgr.file_mgr import FileMgr
from src.mgr.path_resolver import PathGrant, PathResolver
from src.mgr.permission_mgr import AuthorizationResult
from src.tools.policy import PathRole


def _make_auth(resolver: PathResolver, target: Path) -> AuthorizationResult:
    """构造一个允许读取 target 的授权结果。"""
    return AuthorizationResult(
        allowed=True,
        source="policy",
        reason="test",
        safe_detail="test",
        path_grants=(
            PathGrant(
                argument="path",
                role=PathRole.READ,
                path=target,
                classification=resolver.classify(target),
            ),
        ),
    )


def _build_tree_root(tmp_path: Path) -> Path:
    """在 tmp_path 下建混合结构，返回待列出的目录。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x")
    (root / "README.md").write_text("x")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x")
    (root / ".env").write_text("x")
    (root / ".hidden_dir").mkdir()
    (root / ".hidden_dir" / "secret.txt").write_text("x")
    return root


def test_default_hides_hidden(tmp_path):
    mgr = FileMgr(workdir=tmp_path, deps=None)
    root = _build_tree_root(tmp_path)
    auth = _make_auth(mgr._path_resolver, root)

    out = mgr.list_directory(str(root), auth)
    assert "src" in out
    assert "README.md" in out
    assert ".git" not in out
    assert ".env" not in out
    assert ".hidden_dir" not in out
    assert "include_hidden=True" in out  # 提示语出现


def test_include_hidden_shows_all(tmp_path):
    mgr = FileMgr(workdir=tmp_path, deps=None)
    root = _build_tree_root(tmp_path)
    auth = _make_auth(mgr._path_resolver, root)

    out = mgr.list_directory(str(root), auth, include_hidden=True)
    assert "src" in out
    assert ".git" in out
    assert ".env" in out
    assert ".hidden_dir" in out
    assert "secret.txt" in out
    assert "include_hidden=True" not in out  # 无省略提示


def test_hidden_root_itself_listed(tmp_path):
    """根部是隐藏目录时，自身应正常列出，仅子条目里的隐藏项被过滤。"""
    mgr = FileMgr(workdir=tmp_path, deps=None)
    hidden_root = tmp_path / ".agent"
    hidden_root.mkdir()
    (hidden_root / "visible.txt").write_text("x")
    (hidden_root / ".inner").write_text("x")
    auth = _make_auth(mgr._path_resolver, hidden_root)

    out = mgr.list_directory(str(hidden_root), auth)
    assert "visible.txt" in out       # 根部正常列出
    assert ".inner" not in out        # 子条目隐藏项仍被过滤
    assert "Error" not in out
