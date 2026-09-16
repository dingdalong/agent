"""LLMMgr 配置、模型发现与启动错误测试。"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from src.llm import LLMConfigurationError, LLMErrorKind
from src.llm.anthropic import AnthropicProvider
from src.llm.base import LLMProvider
from src.llm.deepseek import DeepSeekProvider
from src.llm.moonshot import MoonshotProvider
from src.llm.ollama import OllamaProvider
from src.llm.openai import OpenAIProvider
from src.mgr.llm_mgr import MODEL_ALIASES, LLMMgr
from src.llm.models import discover_models, normalize_provider_configs, split_model_reference


@pytest.mark.parametrize("reference", ["", "model", "/model", "openai/", "openai /model", 1, None])
def test_model_reference_requires_provider_and_model(reference):
    with pytest.raises(LLMConfigurationError):
        split_model_reference(reference)


def test_model_reference_preserves_model_namespace():
    assert split_model_reference("openai/org/model") == ("openai", "org/model")


def test_unlisted_model_routes_without_discovery(monkeypatch):
    discovery = AsyncMock(side_effect=AssertionError("不得发现模型"))
    monkeypatch.setattr(OpenAIProvider, "list_models", discovery)
    factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: factory)
    manager = _manager()
    assert manager.get().model == "model-a"
    assert manager.get("fast").model == "model-f"
    assert manager.get("openai/org/not-listed").model == "org/not-listed"
    assert manager.get("openai/org/not-listed").provider_name == "openai"
    manager.reconfigure()
    assert manager.get().model == "model-a"
    discovery.assert_not_called()


@pytest.mark.parametrize("is_subagent, requested", [(False, None), (True, "fast"), (True, "openai/custom")])
def test_agent_construction_does_not_discover_models(tmp_path, monkeypatch, is_subagent, requested):
    from src.agent import Agent, AgentDeps

    discovery = AsyncMock(side_effect=AssertionError("启动不得发现模型"))
    monkeypatch.setattr(OpenAIProvider, "list_models", discovery)
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: lambda **kwargs: SimpleNamespace(**kwargs))
    config = _base_config()
    config["compact"] = {"auto_compact_rate": 0.8}
    manager = _manager(config)
    deps = AgentDeps(
        config_mgr=manager.config_mgr, llm_mgr=manager, workdir=tmp_path,
        tools_mgr=SimpleNamespace(schemas=lambda: []),
    )
    agent = Agent(
        agent_type="test", description="test", deps=deps,
        is_subagent=is_subagent, model=requested, features=set(), tools=set(),
    )
    assert agent.model == manager.resolve_model(requested)
    assert agent.llm.provider_name == "openai"
    discovery.assert_not_called()


def test_refresh_merges_configuration_discovery_and_selected_models(monkeypatch):
    config = _base_config()
    config["llm_provider"]["openai"]["models"] = ["local-only", "shared", "shared"]
    discovery = AsyncMock(return_value=["remote-only", "shared", "remote-only"])
    monkeypatch.setattr(OpenAIProvider, "list_models", discovery)
    manager = _manager(config)
    marker = object()
    manager._cache["openai/model-a"] = marker
    asyncio.run(manager.refresh_models())
    assert manager.list_models() == [
        "openai/local-only", "openai/model-a", "openai/model-f", "openai/remote-only", "openai/shared",
    ]
    assert manager.get() is marker
    assert manager.provider_errors == {}
    assert discovery.call_args.kwargs["timeout"] == 3.0
    assert config["llm_provider"]["openai"]["models"] == ["local-only", "shared", "shared"]


@pytest.mark.parametrize("result", [[], ["valid", None], [""], "bad", TimeoutError("offline")])
def test_failed_or_empty_discovery_keeps_configured_and_selected_models(monkeypatch, result):
    config = _base_config()
    config["llm_provider"]["openai"]["models"] = ["configured"]
    discovery = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(OpenAIProvider, "list_models", discovery)
    manager = _manager(config)
    asyncio.run(manager.refresh_models())
    assert manager.list_models() == ["openai/configured", "openai/model-a", "openai/model-f"]
    assert bool(manager.provider_errors) == (result != [])
    assert manager.resolve_model("openai/absent") == "openai/absent"


def test_provider_identity_separates_same_model_and_web_route(monkeypatch):
    config = _base_config()
    config["llm_provider"]["openai"].update(models=["shared"], web="provider")
    config["llm_provider"]["ollama"] = {"base_url": "http://localhost/v1", "models": ["shared"]}
    factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: factory)
    manager = _manager(config)
    first = manager.get("openai/shared")
    second = manager.get("ollama/shared")
    assert first is not second
    assert first.model == second.model == "shared"
    assert first.provider_name == "openai"
    assert second.provider_name == "ollama"
    assert manager.web_mode_for_provider(first.provider_name) == "provider"
    assert manager.web_mode_for_provider(second.provider_name) == "local"
    assert "openai/shared" in manager.list_models()
    assert "ollama/shared" in manager.list_models()


def test_reconfigure_discards_old_endpoint_and_discovery(monkeypatch):
    config = _base_config()
    factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: factory)
    monkeypatch.setattr(OpenAIProvider, "list_models", AsyncMock(return_value=["remote"]))
    manager = _manager(config)
    previous = manager.get()
    asyncio.run(manager.refresh_models())
    config["llm_provider"]["openai"]["base_url"] = "https://new.test/v1"
    manager.reconfigure()
    assert manager.get() is not previous
    assert manager.get().base_url == "https://new.test/v1"
    assert "openai/remote" not in manager.list_models()
    assert manager.provider_errors == {}


def test_invalid_reconfigure_preserves_manager_state():
    config = _base_config()
    manager = _manager(config)
    previous = manager._providers
    marker = object()
    manager._cache["openai/model-a"] = marker
    config["llm_provider"]["openai"]["base_url"] = ""
    with pytest.raises(LLMConfigurationError):
        manager.reconfigure()
    assert manager._providers is previous
    assert manager.get() is marker


def test_refresh_cannot_publish_old_endpoint_results_after_reconfigure(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(**kwargs):
            started.set()
            await release.wait()
            return ["old-endpoint-model"]

        monkeypatch.setattr(OpenAIProvider, "list_models", fetch)
        manager = _manager()
        pending = asyncio.create_task(manager.refresh_models())
        await started.wait()
        manager.reconfigure()
        release.set()
        await pending
        assert "openai/old-endpoint-model" not in manager.list_models()

    asyncio.run(scenario())


def test_discovery_timeout_and_cancellation(monkeypatch):
    async def scenario():
        cancelled = asyncio.Event()

        async def fetch(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(OpenAIProvider, "list_models", fetch)
        config = {"base_url": "https://example.test", "models": ["configured"]}
        result = await discover_models("openai", config, timeout=0.01)
        assert result.models == ["configured"]
        assert result.error.kind is LLMErrorKind.TIMEOUT
        assert cancelled.is_set()
        pending = asyncio.create_task(discover_models("openai", config))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())


def test_discovery_failure_redacts_credentials(monkeypatch, caplog):
    monkeypatch.setattr(OpenAIProvider, "list_models", AsyncMock(side_effect=RuntimeError("api_key=sk-secret-value")))
    result = asyncio.run(discover_models("openai", {"base_url": "https://example.test"}))
    assert result.error is not None
    assert "sk-secret-value" not in result.error.message
    assert "sk-secret-value" not in caplog.text


@pytest.mark.parametrize("providers", [None, {"unknown": {"base_url": "https://example.test"}}, {"openai": {"base_url": "https://example.test", "models": "bad"}}])
def test_invalid_provider_configuration_remains_an_error(providers):
    with pytest.raises(LLMConfigurationError):
        normalize_provider_configs(providers)


def test_unlisted_model_reaches_supplier_and_preserves_supplier_error(monkeypatch):
    import httpx
    from openai import NotFoundError
    from src.llm.errors import LLMCallError

    async def scenario():
        discovery = AsyncMock(side_effect=AssertionError("不得发现模型"))
        monkeypatch.setattr(OpenAIProvider, "list_models", discovery)
        manager = _manager()
        provider = manager.get("openai/org/missing")
        provider.event_bus = SimpleNamespace(emit=AsyncMock())
        monkeypatch.setattr(provider, "estimate_tokens", lambda *args, **kwargs: 1)
        response = httpx.Response(404, request=httpx.Request("POST", "https://example.test/v1/responses"))
        create = AsyncMock(side_effect=NotFoundError(
            "model not found", response=response,
            body={"error": {"message": "model not found", "code": "model_not_found"}},
        ))
        monkeypatch.setattr(provider._client.responses, "create", create)
        try:
            for _attempt in range(2):
                with pytest.raises(LLMCallError) as failure:
                    await provider.chat([{"role": "user", "content": "hello"}])
                assert failure.value.info.status_code == 404
                assert create.call_args.kwargs["model"] == "org/missing"
            assert create.await_count == 2
            discovery.assert_not_called()
        finally:
            await provider._client.close()

    asyncio.run(scenario())


class ConfigStub:
    """提供点路径读取的最小配置管理器。"""

    # 取真实长度量级的绝对路径：LLMConfigurationError 会把消息限长到 500 字符，
    # 短路径无法暴露截断风险。
    global_config_path = Path("/Users/example-user/.agent/config.yaml")
    project_config_path = Path("/Users/example-user/workspace/example-project/.agent/config.yaml")

    def __init__(self, config: dict[str, Any]) -> None:
        """保存测试配置。

        Args:
            config: 完整测试配置。

        Returns:
            None。
        """
        self.config = config

    def get_config(self, key: str) -> Any:
        """按点路径返回配置值。

        Args:
            key: 点分隔配置键。

        Returns:
            目标配置值。
        """
        value: Any = self.config
        for part in key.split("."):
            value = value[part]
        return value

    def get_config_parts(self, parts: tuple[str, ...]) -> Any:
        """按原样路径段返回配置值。"""
        value: Any = self.config
        for part in parts:
            value = value[part]
        return value

class RoleMgrStub:
    """提供 LLMMgr 所需的最小激活角色名接口。"""

    def __init__(self, role_name: str | None = "coding") -> None:
        """保存激活角色名。

        Args:
            role_name: 激活角色名；None 表示没有角色被激活。

        Returns:
            None。
        """
        self.role_name = role_name



def _base_config() -> dict[str, Any]:
    """返回包含完整 LLM 默认值的测试配置。

    Returns:
        可独立修改的配置字典。
    """
    return {
        "llm": {
            "concurrency": 5,
            "timeout_seconds": 120,
            "retry": {
                "max_attempts": 10,
                "base_delay_seconds": 2,
                "max_delay_seconds": 300,
            },
            "user_agent": "agent-test",
        },
        "llm_provider": {
            "openai": {
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
            },
        },
        "role": {
            "default": "coding",
            "coding": {
                "model": {"default": "openai/model-a", "fast": "openai/model-f"},
            },
        },
        "tool": {"page_token_rate": 0.03},
    }


def _manager(
    config: dict[str, Any] | None = None,
    *,
    role_name: str | None = "coding",
) -> LLMMgr:
    """构造不连接网络的 LLM 管理器。

    Args:
        config: 可选完整配置；缺省时使用合法默认配置。
        role_name: 激活角色名，决定读取哪个角色的模型槽位。

    Returns:
        已完成配置解析的管理器。
    """
    return LLMMgr(
        config_mgr=ConfigStub(config or _base_config()),
        role_mgr=RoleMgrStub(role_name),
        event_bus=None,
    )


def _set_path(config: dict[str, Any], path: str, value: Any) -> None:
    """设置测试配置的点路径值。

    Args:
        config: 待修改配置。
        path: 点分隔键路径。
        value: 新配置值。

    Returns:
        None。
    """
    target: dict[str, Any] = config
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "expected_key"),
    [
        ("llm.concurrency", True, "llm.concurrency"),
        ("llm.concurrency", 0, "llm.concurrency"),
        ("llm.concurrency", -1, "llm.concurrency"),
        ("llm.concurrency", 1.5, "llm.concurrency"),
        ("llm.concurrency", "5", "llm.concurrency"),
        ("llm.timeout_seconds", False, "llm.timeout_seconds"),
        ("llm.timeout_seconds", 0, "llm.timeout_seconds"),
        ("llm.timeout_seconds", -1, "llm.timeout_seconds"),
        ("llm.timeout_seconds", math.nan, "llm.timeout_seconds"),
        ("llm.timeout_seconds", math.inf, "llm.timeout_seconds"),
        ("llm.timeout_seconds", -math.inf, "llm.timeout_seconds"),
        ("llm.timeout_seconds", "120", "llm.timeout_seconds"),
        ("llm.retry", None, "llm.retry"),
        ("llm.retry", [], "llm.retry"),
        ("llm.retry", 3, "llm.retry"),
        ("llm.retry.max_attempts", True, "llm.retry.max_attempts"),
        ("llm.retry.max_attempts", 0, "llm.retry.max_attempts"),
        ("llm.retry.max_attempts", -1, "llm.retry.max_attempts"),
        ("llm.retry.max_attempts", 1.5, "llm.retry.max_attempts"),
        ("llm.retry.base_delay_seconds", True, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", 0, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", -1, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", math.nan, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", math.inf, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", -math.inf, "llm.retry.base_delay_seconds"),
        ("llm.retry.base_delay_seconds", "2", "llm.retry.base_delay_seconds"),
        ("llm.retry.max_delay_seconds", False, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", 0, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", -1, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", math.nan, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", math.inf, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", -math.inf, "llm.retry.max_delay_seconds"),
        ("llm.retry.max_delay_seconds", "60", "llm.retry.max_delay_seconds"),
    ],
)
def test_manager_rejects_invalid_llm_configuration(
    path: str,
    value: Any,
    expected_key: str,
) -> None:
    """非法 LLM 配置应抛出包含精确键名的统一配置异常。

    Args:
        path: 待覆盖配置路径。
        value: 非法配置值。
        expected_key: 错误消息必须包含的精确键名。

    Returns:
        None。
    """
    config = _base_config()
    _set_path(config, path, value)

    with pytest.raises(LLMConfigurationError) as exc_info:
        _manager(config)

    assert expected_key in exc_info.value.info.message


def test_manager_rejects_max_delay_below_base_delay() -> None:
    """最大退避小于基础退避时应指向 max_delay_seconds。

    Returns:
        None。
    """
    config = _base_config()
    config["llm"]["retry"] = {
        "max_attempts": 3,
        "base_delay_seconds": 10,
        "max_delay_seconds": 9,
    }

    with pytest.raises(LLMConfigurationError) as exc_info:
        _manager(config)

    assert "llm.retry.max_delay_seconds" in exc_info.value.info.message


def test_manager_rejects_legacy_max_retries_key() -> None:
    """旧 max_retries 键不得被忽略或兼容。

    Returns:
        None。
    """
    config = _base_config()
    config["llm"]["max_retries"] = 3

    with pytest.raises(LLMConfigurationError) as exc_info:
        _manager(config)

    assert "llm.max_retries" in exc_info.value.info.message


def test_manager_uses_interface_defaults_for_missing_optional_keys() -> None:
    """缺少新配置键时应使用 provider 接口默认值。

    Returns:
        None。
    """
    config = _base_config()
    config["llm"] = {"default": "openai/model-a"}

    manager = _manager(config)

    assert manager._default_concurrency == 5
    assert manager._request_timeout_seconds == 120.0
    assert manager._retry_config.max_attempts == 10
    assert manager._retry_config.base_delay_seconds == 2.0
    assert manager._retry_config.max_delay_seconds == 300.0


def test_builtin_config_declares_complete_retry_and_timeout_values() -> None:
    """内置配置应完整声明超时与统一重试参数。

    Returns:
        None。
    """
    config_text = Path("src/config.yaml").read_text()
    config = yaml.safe_load(config_text)

    assert config["llm"]["timeout_seconds"] == 120
    assert "timeout_seconds: 120 # 单次 LLM 请求超时秒数" in config_text
    assert config["llm"]["retry"] == {
        "max_attempts": 10,
        "base_delay_seconds": 2,
        "max_delay_seconds": 300,
    }
    assert "max_retries" not in config["llm"]


def test_builtin_config_declares_anthropic_pause_turn_limit() -> None:
    """内置配置应显式声明 Anthropic pause_turn 续接上限。

    Returns:
        None。
    """
    config = yaml.safe_load(Path("src/config.yaml").read_text())

    assert config["llm_provider"]["anthropic"]["max_pause_turn_continuations"] == 5


def test_builtin_config_omits_provider_reasoning_efforts() -> None:
    """Provider effort 使用类默认值，不应出现在内置配置中。"""
    config = yaml.safe_load(Path("src/config.yaml").read_text())
    provider_configs = config["llm_provider"]

    assert set(provider_configs) == {
        "anthropic",
        "deepseek",
        "moonshot",
        "ollama",
        "openai",
    }
    for provider_config in provider_configs.values():
        assert "reasoning_effort" not in provider_config


@pytest.mark.parametrize(
    "value",
    [
        False,
        True,
        0,
        -1,
        5.0,
        "5",
        None,
        math.nan,
        math.inf,
        -math.inf,
    ],
)
def test_manager_rejects_invalid_anthropic_pause_turn_limit(
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    """Anthropic pause_turn 续接上限只接受非 bool 正整数。

    Args:
        monkeypatch: pytest 属性替换工具。
        value: 待校验的非法配置值。

    Returns:
        None。
    """
    config = _base_config()
    config["llm_provider"] = {
        "anthropic": {
            "base_url": "https://api.anthropic.test",
            "max_pause_turn_continuations": value,
        }
    }
    with pytest.raises(LLMConfigurationError) as exc_info:
        _manager(config)

    assert (
        "llm_provider.anthropic.max_pause_turn_continuations"
        in exc_info.value.info.message
    )


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    [(None, 5), (8, 8)],
)
def test_anthropic_provider_receives_default_or_explicit_pause_turn_limit(
    monkeypatch: pytest.MonkeyPatch,
    configured_limit: int | None,
    expected_limit: int,
) -> None:
    """LLMMgr 应向 Anthropic provider 传入已校验的续接上限。

    Args:
        monkeypatch: pytest 属性替换工具。
        configured_limit: 显式配置值；None 表示省略该键。
        expected_limit: provider 应收到的最终值。

    Returns:
        None。
    """
    captured: dict[str, Any] = {}

    class CapturingProvider:
        """记录构造参数的测试 provider。"""

        def __init__(self, **kwargs: Any) -> None:
            """保存 LLMMgr 下发的构造参数。

            Args:
                kwargs: provider 构造参数。

            Returns:
                None。
            """
            captured.update(kwargs)

    provider_config: dict[str, Any] = {
        "base_url": "https://api.anthropic.test",
    }
    if configured_limit is not None:
        provider_config["max_pause_turn_continuations"] = configured_limit
    config = _base_config()
    config["llm_provider"] = {"anthropic": provider_config}
    manager = _manager(config)
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: CapturingProvider)

    manager.get("anthropic/model-a")

    assert captured["max_pause_turn_continuations"] == expected_limit


def test_provider_creation_receives_validated_runtime_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 创建应接收统一并发、超时和重试参数。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    captured: dict[str, Any] = {}

    class CapturingProvider:
        """记录构造参数的测试 provider。"""

        def __init__(self, **kwargs: Any) -> None:
            """保存所有构造参数。

            Args:
                kwargs: LLMMgr 下发的 provider 参数。

            Returns:
                None。
            """
            captured.update(kwargs)

    config = _base_config()
    config["llm"].update({"concurrency": 7, "timeout_seconds": 45})
    config["llm"]["retry"] = {
        "max_attempts": 4,
        "base_delay_seconds": 1.5,
        "max_delay_seconds": 22,
    }
    manager = _manager(config)
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: CapturingProvider)

    manager.get("openai/model-a")

    assert captured["concurrency"] == 7
    assert captured["timeout"] == 45.0
    assert captured["max_attempts"] == 4
    assert captured["base_delay_seconds"] == 1.5
    assert captured["max_delay_seconds"] == 22.0
    assert "max_retries" not in captured


@pytest.mark.parametrize(
    "configured_effort", [None, "low", "ultra", True, 1, [], {}],
)
def test_provider_creation_does_not_consume_configured_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    configured_effort: Any,
) -> None:
    """配置中的同名残留键不校验、不传入 Provider 构造器。"""
    provider_factory = Mock()
    config = _base_config()
    if configured_effort is not None:
        config["llm_provider"]["openai"]["reasoning_effort"] = configured_effort
    manager = _manager(config)
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: provider_factory)

    manager.get("openai/model-a")

    assert "reasoning_effort" not in provider_factory.call_args.kwargs


@pytest.mark.parametrize(
    ("provider_class", "client_path"),
    [
        (OpenAIProvider, "src.llm.openai.AsyncOpenAI"),
        (DeepSeekProvider, "src.llm.deepseek.AsyncOpenAI"),
        (MoonshotProvider, "src.llm.moonshot.AsyncOpenAI"),
        (OllamaProvider, "src.llm.ollama.AsyncOpenAI"),
        (AnthropicProvider, "src.llm.anthropic.AsyncAnthropic"),
    ],
)
def test_provider_sdk_clients_disable_builtin_retries(
    monkeypatch: pytest.MonkeyPatch,
    provider_class: type[LLMProvider],
    client_path: str,
) -> None:
    """五个 provider 的 SDK client 应使用统一超时并禁用内建重试。

    Args:
        monkeypatch: pytest 属性替换工具。
        provider_class: 待初始化 provider 类型。
        client_path: SDK client 工厂点路径。

    Returns:
        None。
    """
    client_factory = Mock()
    monkeypatch.setattr(client_path, client_factory)

    provider_class(
        api_key="test",
        base_url="https://example.test/v1",
        model="openai/model-a",
        event_bus=None,
        timeout=37,
    )

    assert client_factory.call_args.kwargs["timeout"] == 37
    assert client_factory.call_args.kwargs["max_retries"] == 0


@pytest.mark.parametrize(
    ("provider_class", "client_path", "expected_pause_limit"),
    [
        (OpenAIProvider, "src.llm.openai.AsyncOpenAI", 0),
        (DeepSeekProvider, "src.llm.deepseek.AsyncOpenAI", 0),
        (MoonshotProvider, "src.llm.moonshot.AsyncOpenAI", 0),
        (OllamaProvider, "src.llm.ollama.AsyncOpenAI", 0),
        (AnthropicProvider, "src.llm.anthropic.AsyncAnthropic", 7),
    ],
)
def test_five_providers_expose_protocol_continuation_limit(
    monkeypatch: pytest.MonkeyPatch,
    provider_class: type[LLMProvider],
    client_path: str,
    expected_pause_limit: int,
) -> None:
    """五个 provider 应按协议原因返回各自的续接上限。

    Args:
        monkeypatch: pytest 属性替换工具。
        provider_class: 待构造的 provider 类型。
        client_path: SDK client 工厂点路径。
        expected_pause_limit: pause_turn 对应的预期上限。

    Returns:
        None。
    """
    monkeypatch.setattr(client_path, Mock())
    provider = provider_class(
        api_key="test",
        base_url="https://example.test/v1",
        model="openai/model-a",
        event_bus=None,
        max_pause_turn_continuations=7,
    )

    assert provider.protocol_continuation_limit("pause_turn") == expected_pause_limit
    assert provider.protocol_continuation_limit("stop") == 0


@pytest.mark.parametrize(
    ("provider_class", "client_path"),
    [
        (LLMProvider, "src.llm.base.openai.AsyncOpenAI"),
        (AnthropicProvider, "src.llm.anthropic.AsyncAnthropic"),
    ],
)
def test_model_discovery_client_uses_requested_timeout(
    monkeypatch: pytest.MonkeyPatch,
    provider_class: type[LLMProvider],
    client_path: str,
) -> None:
    """基类与 Anthropic 模型发现应将传入超时用于 SDK 和外层等待。

    Args:
        monkeypatch: pytest 属性替换工具。
        provider_class: 待调用模型发现方法的 provider 类型。
        client_path: SDK client 工厂点路径。

    Returns:
        None。
    """
    captured_wait_timeouts: list[float] = []

    async def capturing_wait_for(awaitable: Any, timeout: float) -> Any:
        """记录外层等待超时并执行原 awaitable。"""
        captured_wait_timeouts.append(timeout)
        return await awaitable

    page = SimpleNamespace(data=[SimpleNamespace(id="openai/model-a")], has_more=False)
    client = SimpleNamespace(
        models=SimpleNamespace(list=AsyncMock(return_value=page)),
        close=AsyncMock(),
    )
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(client_path, client_factory)
    monkeypatch.setattr(asyncio, "wait_for", capturing_wait_for)

    models = asyncio.run(provider_class.list_models(
        api_key="test",
        base_url="https://example.test/v1",
        timeout=19,
    ))

    assert models == ["openai/model-a"]
    assert client_factory.call_args.kwargs["timeout"] == 19
    assert client_factory.call_args.kwargs["max_retries"] == 0
    assert captured_wait_timeouts == [19]


def test_model_aliases_export_covers_slots_and_claudecode_names() -> None:
    """导出的别名集合只含两个槽位与三个 Claude Code 名，不得含 best。"""
    assert set(MODEL_ALIASES) == {"default", "fast", "opus", "sonnet", "haiku"}


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, "openai/model-a"),
        ("", "openai/model-a"),
        ("default", "openai/model-a"),
        ("fast", "openai/model-f"),
        ("opus", "openai/model-a"),
        ("sonnet", "openai/model-a"),
        ("haiku", "openai/model-f"),
        ("openai/model-a", "openai/model-a"),
        ("openai/model-f", "openai/model-f"),
    ],
)
def test_resolve_model_maps_aliases_to_role_slots(
    requested: str | None,
    expected: str,
) -> None:
    """空值、槽位名与 Claude Code 别名都应解析到当前角色的槽位模型。

    Args:
        requested: 传入 resolve_model 的原始值。
        expected: 期望解析出的真实模型 ID。

    Returns:
        None。
    """
    manager = _manager()

    assert manager.resolve_model(requested) == expected


def test_resolve_model_falls_back_to_default_role_without_active_role() -> None:
    """RoleMgr 暂无活动角色名时，槽位解析应回退到 DEFAULT_ROLE。"""
    manager = _manager(role_name=None)

    assert manager.resolve_model("default") == "openai/model-a"
    assert manager.resolve_model("fast") == "openai/model-f"


def test_resolve_model_reads_slots_of_active_role_only() -> None:
    """槽位来源必须是 RoleMgr 给出的激活角色，而非其它角色。

    Returns:
        None。
    """
    config = _base_config()
    config["role"]["reviewer"] = {
        "model": {"default": "openai/model-f", "fast": "openai/model-a"},
    }
    manager = _manager(config, role_name="reviewer")

    assert manager.resolve_model("default") == "openai/model-f"
    assert manager.resolve_model("fast") == "openai/model-a"


def test_resolve_model_rereads_slots_on_every_call() -> None:
    """槽位配置变更后无需 reconfigure 即可实时生效。

    ConfigStub 直接读活字典，等价于 config_mgr.reload() 之后的效果。

    Returns:
        None。
    """
    config = _base_config()
    manager = _manager(config)
    assert manager.resolve_model("fast") == "openai/model-f"

    config["role"]["coding"]["model"]["fast"] = "openai/model-a"

    assert manager.resolve_model("fast") == "openai/model-a"


@pytest.mark.parametrize("broken", ["missing", "legacy-scalar"])
def test_resolve_model_keeps_exact_model_id_when_slots_are_broken(
    broken: str,
) -> None:
    """传入完整模型 ID 时不得触碰槽位配置。

    Args:
        broken: 槽位配置的破坏方式。

    Returns:
        None。
    """
    config = _base_config()
    if broken == "missing":
        del config["role"]["coding"]["model"]
    else:
        config["role"]["coding"]["model"] = "claude-opus-5"
    manager = _manager(config)

    assert manager.resolve_model("openai/model-a") == "openai/model-a"
    assert manager.resolve_model("openai/model-f") == "openai/model-f"


@pytest.mark.parametrize(
    "requested",
    ["best", "inherit", "model-", "model", "unknown-model"],
)
def test_resolve_model_rejects_unknown_names_without_fuzzy_or_fallback(
    requested: str,
) -> None:
    """废弃别名、子串与未知模型名都必须报错，不模糊匹配也不回退 default。

    Args:
        requested: 传入 resolve_model 的非法值。

    Returns:
        None。
    """
    manager = _manager()

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model(requested)

    message = str(exc_info.value)
    assert requested in message


@pytest.mark.parametrize(
    "role_name", ["secret", "token", "password"],
)
def test_slot_help_preserves_sensitive_role_name_yaml_key(
    role_name: str,
) -> None:
    """敏感词角色名经错误清洗后仍应保留完整、可解析的 YAML 样例。

    Args:
        role_name: 会命中凭据清洗关键字的合法角色名。
    Returns:
        None。
    """
    config = _base_config()
    config["role"][role_name] = {}
    manager = _manager(config, role_name=role_name)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("default")

    message = exc_info.value.info.message
    prefix, separator, remainder = message.partition("YAML 样例：")
    assert separator, prefix
    flow_yaml, separator, _suffix = remainder.partition("。")
    assert separator, remainder
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
                    "default": "<供应商>/<模型ID>",
                    "fast": "<供应商>/<模型ID>",
                }
            }
        }
    }
    assert f'role["{role_name}"].model.default' in message
    assert f'role["{role_name}"].model.fast' in message
    assert "[REDACTED]" not in message
    assert len(message) < 500
    assert not message.endswith(("…", "..."))


def test_missing_role_model_config_reports_actionable_error() -> None:
    """角色模型配置缺失时应给出完整键名、YAML 样例与配置文件路径。

    Returns:
        None。
    """
    config = _base_config()
    del config["role"]["coding"]["model"]
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("default")

    message = exc_info.value.info.message
    assert 'role["coding"].model.default' in message
    assert 'role["coding"].model.fast' in message
    assert str(ConfigStub.global_config_path) in message
    assert str(ConfigStub.project_config_path) in message
    assert "未信任" in message
    assert "api_key" not in message


def test_scalar_role_model_is_invalid() -> None:
    """模型槽位必须使用 mapping。

    Returns:
        None。
    """
    config = _base_config()
    config["role"]["coding"]["model"] = "claude-opus-5"
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("fast")

    message = exc_info.value.info.message
    assert "mapping" in message
    assert 'role["coding"].model.fast' in message
    assert "default:" in message and "fast:" in message


def test_role_model_must_be_mapping() -> None:
    """角色模型配置为非 mapping 非 str 时应报错。

    Returns:
        None。
    """
    config = _base_config()
    config["role"]["coding"]["model"] = ["openai/model-a", "openai/model-f"]
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("default")

    assert 'role["coding"].model' in exc_info.value.info.message


@pytest.mark.parametrize("missing_slot", ["default", "fast"])
def test_missing_single_slot_names_that_slot(missing_slot: str) -> None:
    """只配置一个槽位时错误消息必须点名缺失的那个槽位。

    Args:
        missing_slot: 被删除的槽位名。

    Returns:
        None。
    """
    config = _base_config()
    del config["role"]["coding"]["model"][missing_slot]
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("default")

    assert f'role["coding"].model.{missing_slot} 未配置' in exc_info.value.info.message


@pytest.mark.parametrize("value", ["", "   ", 1, 1.5, True, False, None, ["openai/model-a"]])
def test_slot_value_must_be_non_empty_string(value: Any) -> None:
    """槽位值必须是非空且非 bool 的字符串。

    Args:
        value: 待校验的非法槽位值。

    Returns:
        None。
    """
    config = _base_config()
    config["role"]["coding"]["model"]["fast"] = value
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        manager.resolve_model("fast")

    assert 'role["coding"].model.fast' in exc_info.value.info.message


def test_builtin_config_has_no_global_model_aliases_or_role_fallback() -> None:
    """内置配置不得再声明全局模型别名或角色模型兜底值。

    Returns:
        None。
    """
    config = yaml.safe_load(Path("src/config.yaml").read_text())

    assert "default" not in config["llm"]
    assert "best" not in config["llm"]
    assert "fast" not in config["llm"]
    assert config["role"]["default"] == "coding"
    assert "model" not in config["role"]["coding"]
