"""src.mgr.frozen 的冻结态适配测试。

clean_env 的两条分支都容易写错且不易察觉：整体删除会误删用户自己设的变量，
不删又会让子进程加载包内动态库。这里把两种改写方式分别钉住。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.mgr import frozen


@pytest.fixture
def fake_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """伪装成冻结产物，资源根目录为 tmp_path。"""
    monkeypatch.setattr(frozen.sys, "frozen", True, raising=False)
    monkeypatch.setattr(frozen.sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


def test_not_frozen_returns_environment_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码运行时原样复制，不做任何改写。"""
    monkeypatch.delattr(frozen.sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lib")
    assert frozen.clean_env()["LD_LIBRARY_PATH"] == "/opt/lib"


def test_restores_from_orig(fake_bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有 _ORIG 时整体还原为原值，并抹掉 _ORIG 本身。"""
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{fake_bundle}{os.pathsep}/opt/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/lib")
    env = frozen.clean_env()
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_strips_only_bundle_entries(fake_bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 _ORIG 时只剔除包内条目，用户自己的条目必须保留。"""
    monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("DYLD_LIBRARY_PATH", f"{fake_bundle}/lib{os.pathsep}/usr/local/lib")
    assert frozen.clean_env()["DYLD_LIBRARY_PATH"] == "/usr/local/lib"


def test_drops_variable_when_all_entries_are_bundled(
    fake_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条目全在包内时删除该变量，而不是留一个空串。"""
    monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("DYLD_LIBRARY_PATH", f"{fake_bundle}/lib")
    assert "DYLD_LIBRARY_PATH" not in frozen.clean_env()


def test_leaves_unrelated_variables_alone(fake_bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不在名单里的变量一律不动——SSL_CERT_FILE 不走 _ORIG 机制，删了就是误删。"""
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/my-ca.pem")
    monkeypatch.setenv("MY_APP_TOKEN", "secret")
    env = frozen.clean_env()
    assert env["SSL_CERT_FILE"] == "/etc/ssl/my-ca.pem"
    assert env["MY_APP_TOKEN"] == "secret"


def test_uses_provided_base(fake_bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """传入 base 时以其为准，不混入 os.environ。"""
    monkeypatch.setenv("ONLY_IN_OS_ENVIRON", "1")
    env = frozen.clean_env({"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin"}


def test_bundled_path_returns_none_when_missing(fake_bundle: Path) -> None:
    """随包资源不存在时返回 None，由调用方走各自的回退链。"""
    assert frozen.bundled_path("nope") is None
    (fake_bundle / "rg").write_text("x", encoding="utf-8")
    assert frozen.bundled_path("rg") == fake_bundle / "rg"


def test_setup_tiktoken_cache_respects_user_setting(
    fake_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户已显式指定缓存目录时不覆盖。"""
    (fake_bundle / "tiktoken_cache").mkdir()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "/my/cache")
    frozen.setup_tiktoken_cache()
    assert os.environ["TIKTOKEN_CACHE_DIR"] == "/my/cache"


def test_setup_tiktoken_cache_points_into_bundle(
    fake_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未指定时指向包内预热缓存。"""
    (fake_bundle / "tiktoken_cache").mkdir()
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    frozen.setup_tiktoken_cache()
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(fake_bundle / "tiktoken_cache")
