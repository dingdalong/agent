"""首次 Provider 配置业务编排、严格验证与安全持久化的功能集中测试。

所有 Provider 调用均注入 stub，不访问网络；不运行真实 SetupApp UI。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from src.app import bootstrap
from src.app.bootstrap import create_app
from src.app.provider_setup import (
    ProviderOption,
    SetupResult,
    build_provider_options,
    maybe_run_provider_setup,
    persist_setup,
    verify_provider,
)
from src.llm import LLMConfigurationError
from src.llm.deepseek import DeepSeekProvider
from src.llm.ollama import OllamaProvider
from src.mgr.config_mgr import ConfigManager
from src.mgr.paths import builtin_root


class _FakeStream:
    """模拟 isatty 结果的终端流。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _set_tty(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """把 sys.stdin/stdout 替换为固定 isatty 结果的伪流。"""
    monkeypatch.setattr(sys, "stdin", _FakeStream(enabled))
    monkeypatch.setattr(sys, "stdout", _FakeStream(enabled))


def _run(coro):
    """在独立事件循环中运行协程并原样传播其异常。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _builtin_provider_names() -> list[str]:
    """返回内置 config.yaml 中 llm_provider 的 provider 名列表。"""
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())
    return list(builtin.get("llm_provider", {}))


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有内置 Provider 的 API_KEY/API_URL 环境变量，避免宿主环境干扰。"""
    for name in _builtin_provider_names():
        for suffix in ("API_KEY", "API_URL"):
            monkeypatch.delenv(f"{name.upper()}_{suffix}", raising=False)


def _manager(tmp_path: Path, *, project_trusted: bool = False) -> ConfigManager:
    return ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=project_trusted,
    )


def _deepseek_result() -> SetupResult:
    """云 Provider 的成功配置结果（含可识别测试 secret）。"""
    return SetupResult(
        provider="deepseek",
        base_url="https://api.deepseek.test/v1",
        api_key="sk-test-123",
        default_model="deepseek-v4-flash",
    )


# ---------- SetupResult ----------


def test_setup_result_repr_hides_api_key():
    """SetupResult 的 repr 不得包含 api_key 字段或 secret。"""
    result = _deepseek_result()

    text = repr(result)
    assert "sk-test-123" not in text
    assert "api_key" not in text
    assert "deepseek" in text


# ---------- build_provider_options ----------


def test_build_provider_options_order_urls_and_key_requirement(tmp_path, monkeypatch):
    """候选保持内置 mapping 顺序，base_url 一致，仅 ollama 不要求 key。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())["llm_provider"]

    options = build_provider_options(manager)

    assert [option.name for option in options] == list(builtin)
    for option in options:
        assert option.base_url == builtin[option.name]["base_url"]
    assert {option.name for option in options if option.requires_key} == {
        "deepseek",
        "openai",
        "anthropic",
        "moonshot",
    }
    ollama = next(option for option in options if option.name == "ollama")
    assert ollama.requires_key is False


def test_build_provider_options_unknown_provider_raises(tmp_path, monkeypatch):
    """配置了未注册 provider 名时给安全配置错误。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm_provider:\n  mystery:\n    base_url: https://mystery.test/v1\n")
    manager = _manager(tmp_path)

    with pytest.raises(LLMConfigurationError, match="未知 provider 名"):
        build_provider_options(manager)


def test_build_provider_options_invalid_base_url_raises(tmp_path, monkeypatch):
    """base_url 非非空 str 时给安全配置错误。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm_provider:\n  deepseek:\n    base_url: 123\n")
    manager = _manager(tmp_path)

    with pytest.raises(LLMConfigurationError, match="base_url 必须是非空 str"):
        build_provider_options(manager)


# ---------- verify_provider ----------


def test_verify_provider_passes_params_and_normalizes(monkeypatch):
    """调用所选 Provider 类自己的 list_models 并透传全部参数；去重后稳定排序。"""
    captured: dict = {}

    async def fake_list_models(api_key, base_url, timeout, user_agent):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        captured["timeout"] = timeout
        captured["user_agent"] = user_agent
        return ["b-model", "a-model", "B-model", "a-model"]

    monkeypatch.setattr(DeepSeekProvider, "list_models", fake_list_models)
    option = ProviderOption(name="deepseek", base_url="https://api.deepseek.test/v1", requires_key=True)

    models = _run(
        verify_provider(
            option,
            "sk-test-123",
            "https://api.deepseek.test/v1",
            timeout=10.0,
            user_agent="ua-test",
        )
    )

    assert captured == {
        "api_key": "sk-test-123",
        "base_url": "https://api.deepseek.test/v1",
        "timeout": 10.0,
        "user_agent": "ua-test",
    }
    assert models == ["a-model", "B-model", "b-model"]


@pytest.mark.parametrize("api_key", [None, ""])
def test_verify_provider_ollama_empty_key_placeholder(monkeypatch, api_key):
    """Ollama 空 key 调用时仅传非秘密占位 "ollama"，返回值正常。"""
    captured: dict = {}

    async def fake_list_models(api_key, base_url, timeout, user_agent):
        captured["api_key"] = api_key
        return ["qwen3.6"]

    monkeypatch.setattr(OllamaProvider, "list_models", fake_list_models)
    option = ProviderOption(name="ollama", base_url="http://127.0.0.1:8001/v1", requires_key=False)

    models = _run(verify_provider(option, api_key, option.base_url))

    assert models == ["qwen3.6"]
    assert captured["api_key"] == "ollama"


@pytest.mark.parametrize(
    "models",
    [
        [],
        ["ok", 42],
        ["ok", ""],
        "not-a-list",
    ],
)
def test_verify_provider_invalid_or_empty_list_raises(monkeypatch, models):
    """空列表或含非法元素的返回值抛安全配置错误。"""
    async def fake_list_models(api_key, base_url, timeout, user_agent):
        return models

    monkeypatch.setattr(DeepSeekProvider, "list_models", fake_list_models)
    option = ProviderOption(name="deepseek", base_url="https://x", requires_key=True)

    with pytest.raises(LLMConfigurationError):
        _run(verify_provider(option, "k", option.base_url))


def test_verify_provider_propagates_plain_exception(monkeypatch):
    """普通 SDK/网络异常原样传播，不在此拼接 secret。"""
    async def fake_list_models(api_key, base_url, timeout, user_agent):
        raise RuntimeError("boom")

    monkeypatch.setattr(DeepSeekProvider, "list_models", fake_list_models)
    option = ProviderOption(name="deepseek", base_url="https://x", requires_key=True)

    with pytest.raises(RuntimeError, match="boom"):
        _run(verify_provider(option, "k", option.base_url))


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()])
def test_verify_provider_propagates_control_flow(monkeypatch, exc):
    """控制流异常（取消/中断/退出）原样传播。"""
    async def fake_list_models(api_key, base_url, timeout, user_agent):
        raise exc

    monkeypatch.setattr(DeepSeekProvider, "list_models", fake_list_models)
    option = ProviderOption(name="deepseek", base_url="https://x", requires_key=True)

    with pytest.raises(type(exc)):
        _run(verify_provider(option, "k", option.base_url))


# ---------- maybe_run_provider_setup ----------


def test_maybe_run_skips_when_explicit_config(tmp_path, monkeypatch):
    """已有显式 Provider 配置时立即返回，绝不构造/import UI。"""
    _clear_provider_env(monkeypatch)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("DEEPSEEK_API_KEY='sk-existing'\n")
    manager = _manager(tmp_path)
    called: list[bool] = []

    async def fake_setup_app(options, verify):
        called.append(True)
        return None

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    ui_imported_before = "src.interfaces.tui.provider_setup" in sys.modules
    _run(maybe_run_provider_setup(manager))

    assert called == []
    # 与本次调用无关的既有导入（如同会话先跑了 TUI 测试）不算违反契约
    assert ("src.interfaces.tui.provider_setup" in sys.modules) == ui_imported_before


def test_maybe_run_non_tty_raises_actionable_message(tmp_path, monkeypatch):
    """非 TTY 时不调用 helper，抛出含实际路径与变量命名的手工配置指引。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, False)
    manager = _manager(tmp_path)
    called: list[bool] = []

    async def fake_setup_app(options, verify):
        called.append(True)
        return None

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert str(tmp_path / "global" / ".env") in message
    assert str(tmp_path / "global" / "config.yaml") in message
    assert "API_URL" in message
    assert "API_KEY" in message
    assert "llm.default" in message
    assert called == []


def test_maybe_run_cancel_no_persist(tmp_path, monkeypatch):
    """helper 返回 None 视为取消：抛错且两个配置文件都不落盘。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    manager = _manager(tmp_path)

    async def fake_setup_app(options, verify):
        return None

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    with pytest.raises(LLMConfigurationError, match="未写入"):
        _run(maybe_run_provider_setup(manager))

    assert not (tmp_path / "global" / "config.yaml").exists()
    assert not (tmp_path / "global" / ".env").exists()


def test_maybe_run_success_persists_cloud_and_reload_visible(tmp_path, monkeypatch):
    """成功路径：verify 接线（user_agent 来自配置、超时 10 秒），云 Provider 同批写
    URL/key，持久化后 reload 可见。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    manager = _manager(tmp_path)
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())
    captured: dict = {}

    async def fake_list_models(api_key, base_url, timeout, user_agent):
        captured["list_models"] = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout,
            "user_agent": user_agent,
        }
        return ["deepseek-v4-flash", "deepseek-v4-pro"]

    async def fake_setup_app(options, verify):
        captured["options"] = options
        deepseek = next(option for option in options if option.name == "deepseek")
        captured["models"] = await verify(deepseek, "sk-test-123", "https://api.deepseek.test/v1")
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)
    monkeypatch.setattr(DeepSeekProvider, "list_models", fake_list_models)

    _run(maybe_run_provider_setup(manager))

    assert captured["list_models"] == {
        "api_key": "sk-test-123",
        "base_url": "https://api.deepseek.test/v1",
        "timeout": 10.0,
        "user_agent": builtin["llm"]["user_agent"],
    }
    assert captured["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert [option.name for option in captured["options"]] == _builtin_provider_names()
    assert dotenv_values(tmp_path / "global" / ".env") == {
        "DEEPSEEK_API_URL": "https://api.deepseek.test/v1",
        "DEEPSEEK_API_KEY": "sk-test-123",
    }
    assert manager.get_config("llm")["default"] == "deepseek-v4-flash"
    providers = manager.get_config("llm_provider")
    assert providers["deepseek"]["base_url"] == "https://api.deepseek.test/v1"
    assert providers["deepseek"]["api_key"] == "sk-test-123"
    assert manager.has_explicit_provider_config() is True


# ---------- persist_setup ----------


def test_persist_ollama_only_writes_url(tmp_path, monkeypatch):
    """Ollama 持久化只写 OLLAMA_API_URL，不写 key。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    result = SetupResult(
        provider="ollama",
        base_url="http://127.0.0.1:8001/v1",
        api_key=None,
        default_model="qwen3.6",
    )

    persist_setup(manager, result)

    env = dotenv_values(tmp_path / "global" / ".env")
    assert env == {"OLLAMA_API_URL": "http://127.0.0.1:8001/v1"}
    assert "OLLAMA_API_KEY" not in env
    assert manager.get_config("llm")["default"] == "qwen3.6"
    assert manager.get_config("llm_provider")["ollama"]["base_url"] == "http://127.0.0.1:8001/v1"


def test_maybe_run_project_default_override_fails_before_env(tmp_path, monkeypatch):
    """可信项目 llm.default 覆盖全局选择时，在写 env 前失败且不产生显式 Provider 配置。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    project_dir = tmp_path / "work" / ".agent"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text("llm:\n  default: project-model\n")
    manager = _manager(tmp_path, project_trusted=True)

    async def fake_setup_app(options, verify):
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert "llm.default" in message
    assert "sk-test-123" not in message
    assert not (tmp_path / "global" / ".env").exists()
    manager.reload()
    assert manager.has_explicit_provider_config() is False


def test_maybe_run_env_write_failure_no_partial_env(tmp_path, monkeypatch):
    """set_global_env 失败时转成安全 LLMConfigurationError（cause 为 OSError），
    .env 不含新 Provider 项，reload 后仍判定为无显式配置。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    manager = _manager(tmp_path)
    env_path = tmp_path / "global" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("UNRELATED=keep\n")

    async def fake_setup_app(options, verify):
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    def boom(values):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "set_global_env", boom)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)
    assert "sk-test-123" not in message
    assert "disk full" not in message
    assert dotenv_values(env_path) == {"UNRELATED": "keep"}
    assert "DEEPSEEK" not in env_path.read_text()
    # 默认模型可能已写，但无 Provider 环境，下次仍进入向导
    assert manager.get_config("llm")["default"] == "deepseek-v4-flash"
    manager.reload()
    assert manager.has_explicit_provider_config() is False


def test_maybe_run_global_llm_scalar_fails_safe_without_env(tmp_path, monkeypatch):
    """全局 config.yaml 的 llm 为标量时：持久化 ValueError 转安全 LLMConfigurationError，
    不创建 .env、不改写配置（main.cli 对该错误干净退出，见 cli_exits 测试）。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.yaml").write_text("llm: scalar\n")
    manager = _manager(tmp_path)

    async def fake_setup_app(options, verify):
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "sk-test-123" not in message
    assert "必须是对象" not in message
    assert "config.yaml" in message
    assert ".env" in message
    assert not (global_dir / ".env").exists()
    assert (global_dir / "config.yaml").read_text() == "llm: scalar\n"


def test_maybe_run_post_check_failure_safe_error(tmp_path, monkeypatch):
    """reload 后置检查不一致时抛不含 secret 的安全错误。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    manager = _manager(tmp_path)

    async def fake_setup_app(options, verify):
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)
    # 模拟 env 写入未生效：post-check 必然发现 base_url 不一致
    monkeypatch.setattr(manager, "set_global_env", lambda values: None)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert "base_url" in message
    assert "sk-test-123" not in message
    env_path = tmp_path / "global" / ".env"
    assert not env_path.exists() or "sk-test-123" not in env_path.read_text()


# ---------- create_app 接线 ----------


def _patch_bootstrap_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 bootstrap 的全局/工作目录隔离到 tmp_path，并固定项目信任确认结果。

    Returns:
        隔离后的工作目录（已创建）。
    """
    global_dir = tmp_path / "global"
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    async def fake_ensure_trusted(confirm=None):
        return True

    monkeypatch.setattr(bootstrap, "global_data_dir", lambda: global_dir)
    monkeypatch.setattr(bootstrap, "resolve_workdir", lambda override=None: work_dir)
    monkeypatch.setattr(
        bootstrap.ProjectTrustGate,
        "ensure_trusted",
        staticmethod(fake_ensure_trusted),
    )
    return work_dir


def test_create_app_invokes_setup_before_downstream_work(tmp_path, monkeypatch):
    """create_app 在 ConfigManager 构造后立即调用向导一次，失败时不留任何下游痕迹。

    向导哨兵收到真实 ConfigManager 并抛 LLMConfigurationError：项目日志目录尚未创建、
    下游关键 Manager 一个都未构造，异常原样传播（由 main.cli 以“启动失败”退出）。
    """
    work_dir = _patch_bootstrap_env(monkeypatch, tmp_path)
    calls: list[ConfigManager] = []

    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("下游构造不应发生")

    monkeypatch.setattr(bootstrap, "DataGuard", Boom)
    monkeypatch.setattr(bootstrap, "RoleMgr", Boom)
    monkeypatch.setattr(bootstrap, "McpMgr", Boom)
    monkeypatch.setattr(bootstrap, "LLMMgr", Boom)

    async def fake_setup(config_mgr):
        calls.append(config_mgr)
        raise LLMConfigurationError("非 TTY 无法启动向导")

    monkeypatch.setattr(bootstrap, "maybe_run_provider_setup", fake_setup)

    with pytest.raises(LLMConfigurationError, match="无法启动向导"):
        asyncio.run(create_app())

    assert len(calls) == 1
    assert isinstance(calls[0], ConfigManager)
    assert calls[0].project_trusted is True
    assert not (work_dir / ".agent" / "logs").exists()


def test_create_app_continues_after_setup_returns(tmp_path, monkeypatch):
    """向导正常返回后 create_app 才继续组装：顺序为 setup -> 日志目录 -> RoleMgr。

    用 RoleMgr 构造哨兵证明 await 完成后才进入下游，不拉起真实 UI/MCP/网络。
    """
    work_dir = _patch_bootstrap_env(monkeypatch, tmp_path)
    order: list[str] = []
    root_handlers = set(logging.root.handlers)
    root_level = logging.root.level

    async def fake_setup(config_mgr):
        order.append("setup")

    monkeypatch.setattr(bootstrap, "maybe_run_provider_setup", fake_setup)

    class RoleBoom:
        def __init__(self, *args, **kwargs):
            order.append("role_mgr")
            raise RuntimeError("RoleMgr 构造哨兵")

    monkeypatch.setattr(bootstrap, "RoleMgr", RoleBoom)

    try:
        with pytest.raises(RuntimeError, match="RoleMgr 构造哨兵"):
            asyncio.run(create_app())
        # setup 的 await 完成后才继续：日志目录在 setup 之后、RoleMgr 之前创建
        assert order == ["setup", "role_mgr"]
        assert (work_dir / ".agent" / "logs").exists()
    finally:
        # 清理 create_app 注册到 root logger 的项目日志 handler，避免跨测试污染
        logging.root.setLevel(root_level)
        for handler in set(logging.root.handlers) - root_handlers:
            logging.root.removeHandler(handler)
            handler.close()

# ---------- 日志不泄密 ----------


def test_no_secret_in_logs_on_success_persist(tmp_path, monkeypatch, caplog):
    """成功持久化路径不得向任何日志记录输出可识别 secret。

    业务模块本身不日志，测试锁定“无泄漏”而非强迫有日志：清空所有捕获记录后，
    逐条（含格式化参数解析后的消息）与汇总文本都不得包含 secret。
    """
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    secret = "sk-leak-guard-123"
    manager = _manager(tmp_path)

    async def fake_setup_app(options, verify):
        return SetupResult(
            provider="deepseek",
            base_url="https://api.deepseek.test/v1",
            api_key=secret,
            default_model="deepseek-v4-flash",
        )

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)
    caplog.set_level(logging.DEBUG)

    _run(maybe_run_provider_setup(manager))

    assert not any(secret in record.getMessage() for record in caplog.records)
    assert secret not in caplog.text


def test_no_secret_in_logs_on_persist_failure(tmp_path, monkeypatch, caplog):
    """持久化失败路径（set_global_env 抛 OSError）也不得向任何日志记录输出 secret。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    secret = "sk-leak-guard-123"
    manager = _manager(tmp_path)

    async def fake_setup_app(options, verify):
        return SetupResult(
            provider="deepseek",
            base_url="https://api.deepseek.test/v1",
            api_key=secret,
            default_model="deepseek-v4-flash",
        )

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    def boom(values):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "set_global_env", boom)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMConfigurationError):
        _run(maybe_run_provider_setup(manager))

    assert not any(secret in record.getMessage() for record in caplog.records)
    assert secret not in caplog.text
