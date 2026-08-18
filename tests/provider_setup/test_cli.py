"""main.cli 在非 TTY 缺 Provider 配置时的真实出口测试。

调用真实 ``main.cli()``，仅把 ``main_module.create_app`` 换成 wrapper：wrapper 内构造
真实 ConfigManager（tmp 目录隔离）并真实 await ``maybe_run_provider_setup``，保留
main.main/cli 的捕获链，不组装下游、不访问网络，也不直接抛 LLMConfigurationError。
"""

from __future__ import annotations

import sys

import pytest
import yaml

import main as main_module
from src.app.provider_setup import maybe_run_provider_setup
from src.mgr.config_mgr import ConfigManager
from src.mgr.paths import builtin_root


class _FakeStream:
    """模拟 isatty 结果的终端流。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有内置 Provider 的 API_KEY/API_URL 环境变量，避免宿主环境干扰。"""
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())
    for name in builtin.get("llm_provider", {}):
        for suffix in ("API_KEY", "API_URL"):
            monkeypatch.delenv(f"{name.upper()}_{suffix}", raising=False)


def _force_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 provider_setup 的 TTY 检查失败。

    优先只替换流对象的 isatty 方法（capsys 下的 sys.stdout 等对象可能不可改，
    此时回退为把 provider_setup 读取的 sys 流整体换成固定 isatty=False 的伪流；
    main.cli 的错误输出走 sys.stderr，仍由 capsys 捕获）。
    """
    for attr in ("stdin", "stdout"):
        stream = getattr(sys, attr)
        try:
            monkeypatch.setattr(stream, "isatty", lambda: False)
        except (AttributeError, TypeError):
            monkeypatch.setattr(sys, attr, _FakeStream(False))


def test_cli_non_tty_provider_setup_exits_cleanly(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实 main.cli() 非 TTY 缺配置：SystemExit(1) + 启动失败指引 + 零落盘。

    断言 stderr 含实际 .env/config.yaml 路径与 API_URL/API_KEY/角色模型双槽位指引、
    无 Traceback；且未创建任何 Provider 配置文件。
    """
    _clear_provider_env(monkeypatch)
    _force_non_tty(monkeypatch)
    global_dir = tmp_path / "global"
    work_dir = tmp_path / "work"

    async def real_create_app(workdir_override=None, *, copy_on_select=None):
        manager = ConfigManager(
            global_dir=global_dir,
            workdir=work_dir,
            project_trusted=True,
        )
        await maybe_run_provider_setup(manager)
        raise AssertionError("非 TTY 下 maybe_run_provider_setup 应当抛 LLMConfigurationError")

    monkeypatch.setattr(main_module, "create_app", real_create_app)
    monkeypatch.setattr(main_module, "setup_tiktoken_cache", lambda: None)
    monkeypatch.setattr("sys.argv", ["main.py"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.cli()

    stderr = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "启动失败" in stderr
    assert str(global_dir / ".env") in stderr
    assert str(global_dir / "config.yaml") in stderr
    assert "API_URL" in stderr
    assert "API_KEY" in stderr
    assert 'role["coding"].model.default' in stderr
    assert 'role["coding"].model.fast' in stderr
    assert "Traceback" not in stderr
    # 失败即止：未创建任何 Provider 配置文件
    assert not (global_dir / ".env").exists()
    assert not (global_dir / "config.yaml").exists()
