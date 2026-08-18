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
    _non_tty_message,
    _persist_failure_message,
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
from src.mgr.role_mgr import DEFAULT_ROLE, RoleMgr


class _FakeStream:
    """模拟 isatty 结果的终端流。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _MessageConfigStub:
    """为异常消息测试提供固定长度路径与可配置角色名。"""

    global_dir = Path("/home/test/.agent")
    workdir = Path("/workspace/project")
    project_trusted = False

    def __init__(self, role_name: str = DEFAULT_ROLE) -> None:
        """保存消息中使用的激活角色名。

        Args:
            role_name: ``role.default`` 返回的角色名。
        """
        self.role_name = role_name

    def get_config(self, key: str):
        """读取消息构造所需的最小配置。

        Args:
            key: 点分隔配置键。

        Returns:
            ``role.default`` 的固定值。

        Raises:
            KeyError: 请求其他配置键时。
        """
        if key == "role.default":
            return self.role_name
        raise KeyError(key)


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


def _write_role(roles_dir: Path, name: str) -> Path:
    """创建可由角色发现与 RoleMgr 解析的最小角色目录。

    Args:
        roles_dir: ``roles`` 目录。
        name: 角色名。

    Returns:
        创建的角色目录路径。
    """
    role_dir = roles_dir / name
    role_dir.mkdir(parents=True)
    (role_dir / "role.md").write_text("---\nagent_type: main\n---\n测试角色。\n")
    return role_dir


def _deepseek_result() -> SetupResult:
    """云 Provider 的成功配置结果（两个槽位取不同模型，含可识别测试 secret）。"""
    return SetupResult(
        provider="deepseek",
        base_url="https://api.deepseek.test/v1",
        api_key="sk-test-123",
        default_model="deepseek-v4-pro",
        fast_model="deepseek-v4-flash",
    )


# _deepseek_result() 期望落到激活角色 model 父键下的两个槽位值。
_EXPECTED_SLOTS = {"default": "deepseek-v4-pro", "fast": "deepseek-v4-flash"}


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


# ---------- 异常清洗后的手工指引 ----------


def _flow_yaml_from_message(message: str) -> str:
    """从配置错误消息中提取流式 YAML 样例。

    Args:
        message: 经 LLMConfigurationError 清洗后的消息。

    Returns:
        ``YAML 样例：`` 与下一个句号之间的 YAML 文本。
    """
    prefix, separator, remainder = message.partition("YAML 样例：")
    assert separator, prefix
    flow_yaml, separator, _suffix = remainder.partition("。")
    assert separator, remainder
    return flow_yaml


def test_non_tty_message_survives_configuration_error_sanitizing():
    """非 TTY 指引经清洗后仍含完整键名、合法流式 YAML 与尾部路径信息。"""
    config_mgr = _MessageConfigStub()
    options = [
        ProviderOption(name, f"https://{name}.test/v1", name != "ollama")
        for name in ("deepseek", "openai", "anthropic", "moonshot", "ollama")
    ]
    message = LLMConfigurationError(
        _non_tty_message(config_mgr, options)
    ).info.message
    flow_yaml = _flow_yaml_from_message(message)
    parsed_key = next(iter(yaml.safe_load(flow_yaml)["role"]))
    print(
        f"role='coding' yaml={flow_yaml} parsed_key={parsed_key!r} "
        f"message_length={len(message)}"
    )

    assert 'role["coding"].model.default' in message
    assert 'role["coding"].model.fast' in message
    assert yaml.safe_load(flow_yaml) == {
        "role": {
            "coding": {
                "model": {
                    "default": "<default-model-id>",
                    "fast": "<fast-model-id>",
                }
            }
        }
    }
    config_path = str(config_mgr.global_dir / "config.yaml")
    env_path = str(config_mgr.global_dir / ".env")
    assert message.index('role["coding"].model.default') < message.index(flow_yaml)
    assert message.index('role["coding"].model.fast') < message.index(flow_yaml)
    assert message.index(flow_yaml) < message.index(config_path) < message.index(env_path)
    assert "候选 Provider：deepseek、openai、anthropic、moonshot、ollama" in message
    assert "配置后重新运行" in message
    assert len(message) < 500
    assert not message.endswith(("…", "..."))


def test_non_tty_message_supports_long_role_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长角色名的流式 YAML 应完整可解析，且消息不触发 500 字符截断。"""
    role_name = "a" * 64
    config_mgr = _MessageConfigStub(role_name)
    monkeypatch.setattr(
        "src.app.provider_setup.discover_roles",
        lambda workdir, global_dir, project_trusted: {
            role_name: global_dir / "roles" / role_name
        },
    )
    options = [
        ProviderOption(name, f"https://{name}.test/v1", name != "ollama")
        for name in ("deepseek", "openai", "anthropic", "moonshot", "ollama")
    ]
    message = LLMConfigurationError(
        _non_tty_message(config_mgr, options)
    ).info.message
    flow_yaml = _flow_yaml_from_message(message)
    parsed_key = next(iter(yaml.safe_load(flow_yaml)["role"]))
    print(
        f"role={role_name!r} yaml={flow_yaml} parsed_key={parsed_key!r} "
        f"message_length={len(message)}"
    )

    assert f'role["{role_name}"].model' in message
    assert yaml.safe_load(flow_yaml) == {
        "role": {
            role_name: {
                "model": {
                    "default": "<default-model-id>",
                    "fast": "<fast-model-id>",
                }
            }
        }
    }
    assert "候选 Provider：deepseek、openai、anthropic、moonshot、ollama" in message
    assert "配置后重新运行" in message
    assert len(message) < 500
    assert not message.endswith(("…", "..."))

@pytest.mark.parametrize(
    "role_name", ["true", "null", "123", "研发.角色"],
)
def test_non_tty_message_preserves_implicit_scalar_role_names(
    monkeypatch: pytest.MonkeyPatch,
    role_name: str,
) -> None:
    """隐式 YAML 标量角色名经异常清洗后仍应解析为原字符串 key。

    Args:
        monkeypatch: pytest 属性替换工具。
        role_name: 会被裸 YAML key 隐式转型的合法角色名。
    Returns:
        None。
    """
    config_mgr = _MessageConfigStub(role_name)
    monkeypatch.setattr(
        "src.app.provider_setup.discover_roles",
        lambda workdir, global_dir, project_trusted: {
            role_name: global_dir / "roles" / role_name
        },
    )
    options = [ProviderOption("ollama", "http://localhost:11434/v1", False)]

    message = LLMConfigurationError(
        _non_tty_message(config_mgr, options)
    ).info.message
    flow_yaml = _flow_yaml_from_message(message)
    parsed = yaml.safe_load(flow_yaml)
    parsed_key = next(iter(parsed["role"]))
    print(
        f"role={role_name!r} yaml={flow_yaml} parsed_key={parsed_key!r} "
        f"message_length={len(message)}"
    )

    assert parsed == {
        "role": {
            role_name: {
                "model": {
                    "default": "<default-model-id>",
                    "fast": "<fast-model-id>",
                }
            }
        }
    }
    assert f'role["{role_name}"].model.default' in message
    assert f'role["{role_name}"].model.fast' in message
    assert len(message) < 500
    assert not message.endswith(("…", "..."))


def test_persist_failure_message_keeps_actions_before_paths_after_sanitizing():
    """持久化失败提示先保留排查动作，固定路径下清洗后完整未截断。"""
    config_mgr = _MessageConfigStub()

    message = LLMConfigurationError(
        _persist_failure_message(config_mgr)
    ).info.message

    config_path = str(config_mgr.global_dir / "config.yaml")
    env_path = str(config_mgr.global_dir / ".env")
    assert "role 与 llm_provider" in message
    assert "目录权限" in message
    assert "磁盘空间" in message
    assert "重新运行配置向导" in message
    assert message.index("role 与 llm_provider") < message.index(config_path)
    assert config_path in message
    assert env_path in message
    assert len(message) < 500
    assert not message.endswith(("…", "..."))


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
    assert 'role["coding"].model.default' in message
    assert 'role["coding"].model.fast' in message
    assert "llm.default" not in message
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
    assert manager.get_config("role.coding.model") == _EXPECTED_SLOTS
    providers = manager.get_config("llm_provider")
    assert providers["deepseek"]["base_url"] == "https://api.deepseek.test/v1"
    assert providers["deepseek"]["api_key"] == "sk-test-123"
    assert manager.has_explicit_provider_config() is True


# ---------- persist_setup ----------


def test_persist_ollama_only_writes_url(tmp_path, monkeypatch):
    """Ollama 持久化只写 OLLAMA_API_URL，不写 key；两个槽位可取同一模型。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    result = SetupResult(
        provider="ollama",
        base_url="http://127.0.0.1:8001/v1",
        api_key=None,
        default_model="qwen3.6",
        fast_model="qwen3.6",
    )

    persist_setup(manager, result)

    env = dotenv_values(tmp_path / "global" / ".env")
    assert env == {"OLLAMA_API_URL": "http://127.0.0.1:8001/v1"}
    assert "OLLAMA_API_KEY" not in env
    assert manager.get_config("role.coding.model") == {
        "default": "qwen3.6",
        "fast": "qwen3.6",
    }
    assert manager.get_config("llm_provider")["ollama"]["base_url"] == "http://127.0.0.1:8001/v1"


def test_persist_setup_writes_dotted_role_as_exact_mapping_key(tmp_path, monkeypatch):
    """setup 写入含点角色时不得创建 role.review.v2 嵌套结构。"""
    _clear_provider_env(monkeypatch)
    role_name = "review.v2"
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _write_role(global_dir / "roles", role_name)
    global_dir.joinpath("config.yaml").write_text(
        f"role:\n  default: {role_name}\n"
    )
    manager = _manager(tmp_path)

    persist_setup(manager, _deepseek_result())

    assert manager.get_config_parts(("role", role_name, "model")) == _EXPECTED_SLOTS
    written = yaml.safe_load(global_dir.joinpath("config.yaml").read_text())
    assert written["role"][role_name]["model"] == _EXPECTED_SLOTS
    assert "review" not in written["role"]


def test_maybe_run_project_slot_override_fails_before_env(tmp_path, monkeypatch):
    """可信项目 role.<角色>.model 覆盖全局选择时，在写 env 前失败且不产生显式 Provider 配置。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    project_dir = tmp_path / "work" / ".agent"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        "role:\n  coding:\n    model:\n      default: project-model\n"
    )
    manager = _manager(tmp_path, project_trusted=True)

    async def fake_setup_app(options, verify):
        return _deepseek_result()

    monkeypatch.setattr("src.app.provider_setup._run_setup_app", fake_setup_app)

    with pytest.raises(LLMConfigurationError) as excinfo:
        _run(maybe_run_provider_setup(manager))

    message = str(excinfo.value)
    assert 'role["coding"].model.default' in message
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
    # 角色模型槽位可能已写，但无 Provider 环境，下次仍进入向导
    assert manager.get_config("role.coding.model") == _EXPECTED_SLOTS
    manager.reload()
    assert manager.has_explicit_provider_config() is False


def test_maybe_run_global_role_scalar_fails_safe_without_env(tmp_path, monkeypatch):
    """全局 config.yaml 的 role 为标量时：持久化 ValueError 转安全 LLMConfigurationError，
    不创建 .env、不改写配置（main.cli 对该错误干净退出，见 cli_exits 测试）。"""
    _clear_provider_env(monkeypatch)
    _set_tty(monkeypatch, True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.yaml").write_text("role: scalar\n")
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
    assert (global_dir / "config.yaml").read_text() == "role: scalar\n"


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
            fast_model="deepseek-v4-flash",
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
            fast_model="deepseek-v4-flash",
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


# ---------- 角色模型双槽位持久化 ----------


def test_persist_writes_role_model_slots_to_global(tmp_path, monkeypatch):
    """两个槽位整体写入全局 role.<激活角色>.model，且不再写 llm.default。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)

    persist_setup(manager, _deepseek_result())

    written = yaml.safe_load((tmp_path / "global" / "config.yaml").read_text())
    assert written["role"]["coding"]["model"] == _EXPECTED_SLOTS
    assert "default" not in written.get("llm", {})
    assert manager.get_config("role.coding.model") == _EXPECTED_SLOTS


def test_persist_missing_configured_role_matches_subsequent_role_mgr(tmp_path, monkeypatch):
    """缺失的 role.default 应在 setup 与随后 RoleMgr 中一致回退到 coding。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("role:\n  default: research\n")
    manager = _manager(tmp_path)

    persist_setup(manager, _deepseek_result())
    role_mgr = RoleMgr(
        config_mgr=manager,
        workdir=manager.workdir,
        global_dir=manager.global_dir,
    )

    written = yaml.safe_load(global_path.read_text())
    assert role_mgr.role_name == DEFAULT_ROLE
    assert written["role"][role_mgr.role_name]["model"] == _EXPECTED_SLOTS
    assert "model" not in written["role"].get("research", {})


@pytest.mark.parametrize(
    ("role_layer", "project_trusted"),
    [("global", False), ("project", True)],
)
def test_persist_keeps_discovered_custom_role(
    tmp_path, monkeypatch, role_layer, project_trusted
):
    """全局或可信项目层存在自定义角色时，setup 不应误回退。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("role:\n  default: research\n")
    roles_dir = (
        tmp_path / "global" / "roles"
        if role_layer == "global"
        else tmp_path / "work" / ".agent" / "roles"
    )
    _write_role(roles_dir, "research")
    manager = _manager(tmp_path, project_trusted=project_trusted)

    persist_setup(manager, _deepseek_result())
    role_mgr = RoleMgr(
        config_mgr=manager,
        workdir=manager.workdir,
        global_dir=manager.global_dir,
    )

    written = yaml.safe_load(global_path.read_text())
    assert role_mgr.role_name == "research"
    assert written["role"][role_mgr.role_name]["model"] == _EXPECTED_SLOTS
    assert "model" not in written["role"].get(DEFAULT_ROLE, {})


@pytest.mark.parametrize("role_default", ['""', "~"])
def test_persist_blank_role_default_falls_back_to_default_role(
    tmp_path, monkeypatch, role_default
):
    """role.default 为空串/空值时回退到 DEFAULT_ROLE。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text(f"role:\n  default: {role_default}\n")
    manager = _manager(tmp_path)

    persist_setup(manager, _deepseek_result())

    written = yaml.safe_load(global_path.read_text())
    assert written["role"][DEFAULT_ROLE]["model"] == _EXPECTED_SLOTS


def test_persist_overwrites_legacy_scalar_role_model(tmp_path, monkeypatch):
    """全局层残留旧标量 role.<角色>.model 时整体覆盖为 mapping，不抛 ValueError。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("role:\n  default: coding\n  coding:\n    model: old-model\n")
    manager = _manager(tmp_path)

    persist_setup(manager, _deepseek_result())

    written = yaml.safe_load(global_path.read_text())
    assert written["role"]["coding"]["model"] == _EXPECTED_SLOTS
    assert manager.get_config("role.coding.model") == _EXPECTED_SLOTS
    assert dotenv_values(tmp_path / "global" / ".env") == {
        "DEEPSEEK_API_URL": "https://api.deepseek.test/v1",
        "DEEPSEEK_API_KEY": "sk-test-123",
    }


@pytest.mark.parametrize("slot", ["default", "fast"])
def test_persist_project_slot_override_fails_before_env(tmp_path, monkeypatch, slot):
    """可信项目覆盖任一槽位时在写 .env 之前抛错，消息说明被更高优先级层覆盖。"""
    _clear_provider_env(monkeypatch)
    project_dir = tmp_path / "work" / ".agent"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        f"role:\n  coding:\n    model:\n      {slot}: project-model\n"
    )
    manager = _manager(tmp_path, project_trusted=True)

    with pytest.raises(LLMConfigurationError) as excinfo:
        persist_setup(manager, _deepseek_result())

    message = str(excinfo.value)
    assert 'role["coding"].model.default' in message
    assert 'role["coding"].model.fast' in message
    assert "覆盖" in message
    assert "sk-test-123" not in message
    assert not (tmp_path / "global" / ".env").exists()


def test_persist_blank_fast_model_rejected_before_any_write(tmp_path, monkeypatch):
    """fast_model 为空白时校验失败，且配置与 .env 都不落盘。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    result = SetupResult(
        provider="deepseek",
        base_url="https://api.deepseek.test/v1",
        api_key="sk-test-123",
        default_model="deepseek-v4-pro",
        fast_model="   ",
    )

    with pytest.raises(LLMConfigurationError, match="fast_model"):
        persist_setup(manager, result)

    assert not (tmp_path / "global" / "config.yaml").exists()
    assert not (tmp_path / "global" / ".env").exists()
