"""冻结产物冒烟测试 — 断言打包后没有发生静默降级。

这些用例只在构建产物存在时才有意义，因此默认 skip；跑 `make build`（或 CI 里
构建完成）之后再执行 `uv run pytest tests/packaging`。

之所以必须对着产物跑而不是跑源码测试：本目录覆盖的每一项在源码运行下都正常，
只有冻结后才失效，且失效方式是「能启动但不干活」而非抛异常。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .conftest import BINARY, requires_build


def _self_check(env_overrides: dict[str, str] | None = None, cwd: Path | None = None) -> dict:
    """在指定环境下运行产物自检并解析 JSON 报告。

    Args:
        env_overrides: 追加/覆盖的环境变量。
        cwd: 运行目录；默认用仓库外的目录，确保不依赖 cwd 相对路径。

    Returns:
        自检报告字典。
    """
    env = {**os.environ, **(env_overrides or {})}
    proc = subprocess.run(
        [str(BINARY), "--self-check"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    assert proc.stdout, f"自检无输出，stderr={proc.stderr[:2000]}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """在仓库外的干净目录运行一次自检，供各用例共享。"""
    return _self_check(cwd=tmp_path_factory.mktemp("clean"))


@requires_build
def test_runs_as_frozen_bundle(report: dict) -> None:
    """产物确实是冻结形态（否则以下断言测的是源码运行，没有意义）。"""
    assert report["frozen"] is True


@requires_build
def test_all_checks_pass(report: dict) -> None:
    """自检整体通过。"""
    assert report["ok"], f"失败项：{report['failed']}\n{json.dumps(report, ensure_ascii=False, indent=2)}"


@requires_build
def test_builtin_tools_registered(report: dict) -> None:
    """内置工具全部注册 —— 冻结后目录 glob 会落空，这里守住 pkgutil 枚举。"""
    tools = report["checks"]["tools"]
    assert tools["count"] == tools["expected"]


@requires_build
def test_builtin_commands_loaded(report: dict) -> None:
    """内置 slash 命令全部加载。"""
    assert report["checks"]["commands"]["missing"] == []


@requires_build
def test_bundled_resources_resolved(report: dict) -> None:
    """builtin_root() 能定位到随包资源，且四个角色都在。"""
    resources = report["checks"]["resources"]
    assert resources["missing"] == []
    assert resources["missing_roles"] == []
    assert "_internal" in resources["builtin_root"]


@requires_build
def test_ripgrep_comes_from_bundle(report: dict) -> None:
    """rg 命中随包副本，而不是碰巧依赖了宿主预装。"""
    ripgrep = report["checks"]["ripgrep"]
    assert ripgrep["ok"]
    assert ripgrep["bundled"], f"rg 未取自包内：{ripgrep['path']}"


@requires_build
def test_deepseek_tokenizer_works(report: dict) -> None:
    """DeepSeek tokenizer 能从随包 tokenizer.json 加载并计数。"""
    assert report["checks"]["deepseek_tokenizer"]["tokens"] > 0


@requires_build
def test_starts_without_network(tmp_path: Path) -> None:
    """断网也能完成启动期的全部工作（tiktoken 编码走随包预热缓存）。

    用不可达代理模拟断网：tiktoken 的下载走 requests，会遵守代理设置。
    """
    blocked = {k: "http://127.0.0.1:1" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    report = _self_check(env_overrides=blocked, cwd=tmp_path)
    assert report["ok"], f"断网自检失败：{report['failed']}"


@requires_build
def test_network_block_is_effective(tmp_path: Path) -> None:
    """反向验证：缓存缺失时断网必须失败。

    否则 test_starts_without_network 可能只是代理没生效的假阳性。
    """
    blocked = {k: "http://127.0.0.1:1" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    blocked["TIKTOKEN_CACHE_DIR"] = str(tmp_path / "empty")
    report = _self_check(env_overrides=blocked, cwd=tmp_path)
    assert "tiktoken" in report["failed"], "断网未被拦截，离线用例不可信"
