"""LLMMgr 配置、模型发现与启动错误测试。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

import main as main_module
from src.llm import LLMConfigurationError, LLMErrorKind
from src.llm.anthropic import AnthropicProvider
from src.llm.base import LLMProvider
from src.llm.deepseek import DeepSeekProvider
from src.llm.moonshot import MoonshotProvider
from src.llm.ollama import OllamaProvider
from src.llm.openai import OpenAIProvider
from src.mgr.llm_mgr import LLMMgr, ModelUnavailableError


class ConfigStub:
    """提供点路径读取的最小配置管理器。"""

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


class DiscoveryError(Exception):
    """模拟携带结构化供应商元数据的模型发现异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        request_id: str,
    ) -> None:
        """初始化测试异常。

        Args:
            message: 供应商结构化错误摘要。
            status_code: HTTP 状态码。
            code: 供应商错误码。
            request_id: 请求 ID。

        Returns:
            None。
        """
        super().__init__(f"unsafe raw body: {message}")
        self.status_code = status_code
        self.body = {"error": {"message": message, "code": code}}
        self.request_id = request_id
        self.response = SimpleNamespace(status_code=status_code, headers={})


def _base_config() -> dict[str, Any]:
    """返回包含完整 LLM 默认值的测试配置。

    Returns:
        可独立修改的配置字典。
    """
    return {
        "llm": {
            "default": "model-a",
            "concurrency": 5,
            "timeout_seconds": 120,
            "retry": {
                "max_attempts": 3,
                "base_delay_seconds": 2,
                "max_delay_seconds": 60,
            },
            "user_agent": "agent-test",
        },
        "llm_provider": {
            "stub": {
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
            },
        },
        "tool": {"page_token_rate": 0.03},
    }


def _manager(config: dict[str, Any] | None = None) -> LLMMgr:
    """构造不连接网络的 LLM 管理器。

    Args:
        config: 可选完整配置；缺省时使用合法默认配置。

    Returns:
        已完成配置解析的管理器。
    """
    return LLMMgr(config_mgr=ConfigStub(config or _base_config()), event_bus=None)


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
    config["llm"] = {"default": "model-a"}

    manager = _manager(config)

    assert manager._default_concurrency == 5
    assert manager._request_timeout_seconds == 120.0
    assert manager._retry_config.max_attempts == 3
    assert manager._retry_config.base_delay_seconds == 2.0
    assert manager._retry_config.max_delay_seconds == 60.0


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
        "max_attempts": 3,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
    }
    assert "max_retries" not in config["llm"]


def test_builtin_config_declares_anthropic_pause_turn_limit() -> None:
    """内置配置应显式声明 Anthropic pause_turn 续接上限。

    Returns:
        None。
    """
    config = yaml.safe_load(Path("src/config.yaml").read_text())

    assert config["llm_provider"]["anthropic"]["max_pause_turn_continuations"] == 5


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
    manager = _manager(config)
    provider_class = SimpleNamespace(list_models=AsyncMock(return_value=[]))
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: provider_class)

    with pytest.raises(LLMConfigurationError) as exc_info:
        asyncio.run(manager.load_models())

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
    manager._model_to_provider["model-a"] = "anthropic"
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: CapturingProvider)

    manager.get("model-a")

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
    manager._model_to_provider["model-a"] = "stub"
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: CapturingProvider)

    manager.get("model-a")

    assert captured["concurrency"] == 7
    assert captured["timeout"] == 45.0
    assert captured["max_attempts"] == 4
    assert captured["base_delay_seconds"] == 1.5
    assert captured["max_delay_seconds"] == 22.0
    assert "max_retries" not in captured


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
        model="model-a",
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
        model="model-a",
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

    page = SimpleNamespace(data=[SimpleNamespace(id="model-a")], has_more=False)
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

    assert models == ["model-a"]
    assert client_factory.call_args.kwargs["timeout"] == 19
    assert client_factory.call_args.kwargs["max_retries"] == 0
    assert captured_wait_timeouts == [19]


def test_load_models_uses_fixed_timeout_and_static_fallback_for_failed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现失败时应保存分类错误并仅注册非空静态模型。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    calls: dict[str, dict[str, Any]] = {}

    class AuthProvider:
        """认证失败且具有静态回退的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """记录参数后抛认证异常。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                DiscoveryError: 固定认证失败。
            """
            calls["auth"] = kwargs
            raise DiscoveryError(
                "invalid api_key=super-secret sk-live-secret",
                status_code=401,
                code="invalid_api_key",
                request_id="req-auth",
            )

    class RateProvider:
        """限流失败且没有静态回退的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """记录参数后抛限流异常。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                DiscoveryError: 固定限流失败。
            """
            calls["rate"] = kwargs
            raise DiscoveryError(
                "too many requests",
                status_code=429,
                code="rate_limit_exceeded",
                request_id="req-rate",
            )

    config = _base_config()
    config["llm"]["default"] = "static-model"
    config["llm"]["timeout_seconds"] = 33
    config["llm_provider"] = {
        "auth": {
            "base_url": "https://auth.example.test/v1",
            "models": ["static-model"],
        },
        "rate": {"base_url": "https://rate.example.test/v1"},
    }
    providers = {"auth": AuthProvider, "rate": RateProvider}
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", providers.__getitem__)
    manager = _manager(config)

    asyncio.run(manager.load_models())

    assert manager.list_models() == ["static-model"]
    manager.ensure_default_available()
    assert calls["auth"]["timeout"] == 3.0
    assert calls["rate"]["timeout"] == 3.0
    assert manager.provider_errors["auth"].kind is LLMErrorKind.AUTHENTICATION
    assert manager.provider_errors["auth"].request_id == "req-auth"
    assert manager.provider_errors["rate"].kind is LLMErrorKind.RATE_LIMIT


def test_unknown_provider_is_configuration_error_even_with_static_default() -> None:
    """未知 provider 名不得被模型发现静态回退掩盖。

    Returns:
        None。
    """
    config = _base_config()
    config["llm"]["default"] = "typo-model"
    config["llm_provider"] = {
        "opneai": {
            "base_url": "https://example.test/v1",
            "models": ["typo-model"],
        }
    }
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        asyncio.run(manager.load_models())

    assert "llm_provider.opneai" in exc_info.value.info.message
    assert manager.list_models() == []
    assert manager.provider_errors == {}


@pytest.mark.parametrize(
    ("providers_config", "expected_key"),
    [
        (None, "llm_provider"),
        ([], "llm_provider"),
        ({1: {"base_url": "https://example.test/v1"}}, "llm_provider"),
        ({"": {"base_url": "https://example.test/v1"}}, "llm_provider"),
        ({"   ": {"base_url": "https://example.test/v1"}}, "llm_provider"),
        ({"openai": []}, "llm_provider.openai"),
        ({"openai": {}}, "llm_provider.openai.base_url"),
        ({"openai": {"base_url": None}}, "llm_provider.openai.base_url"),
        ({"openai": {"base_url": ""}}, "llm_provider.openai.base_url"),
        ({"openai": {"base_url": "   "}}, "llm_provider.openai.base_url"),
        (
            {"openai": {"base_url": "https://example.test/v1", "models": "model-a"}},
            "llm_provider.openai.models",
        ),
        (
            {"openai": {"base_url": "https://example.test/v1", "models": [""]}},
            "llm_provider.openai.models[0]",
        ),
        (
            {"openai": {"base_url": "https://example.test/v1", "models": ["   "]}},
            "llm_provider.openai.models[0]",
        ),
        (
            {"openai": {"base_url": "https://example.test/v1", "models": [1]}},
            "llm_provider.openai.models[0]",
        ),
    ],
)
def test_load_models_validates_provider_configuration_before_discovery(
    providers_config: Any,
    expected_key: str,
) -> None:
    """provider 配置结构错误应在任何发现任务前统一失败。

    Args:
        providers_config: 待验证 llm_provider 配置。
        expected_key: 配置异常必须包含的精确键名。

    Returns:
        None。
    """
    config = _base_config()
    config["llm_provider"] = providers_config
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        asyncio.run(manager.load_models())

    assert expected_key in exc_info.value.info.message
    assert manager.list_models() == []
    assert manager.provider_errors == {}


def test_static_models_are_deduplicated_within_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 provider 的重复静态模型应按首次出现顺序去重。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    class FailedProvider:
        """固定发现失败的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出网络错误以触发静态回退。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                ConnectionError: 固定网络错误。
            """
            raise ConnectionError("connection refused")

    config = _base_config()
    config["llm_provider"]["stub"]["models"] = ["model-a", "model-a", "model-b"]
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: FailedProvider)
    manager = _manager(config)

    asyncio.run(manager.load_models())

    assert manager.list_models() == ["model-a", "model-b"]


@pytest.mark.parametrize(
    "api_models",
    [None, ("model-a",), "model-a", [""], ["   "], [1]],
)
def test_invalid_api_model_list_is_protocol_error_with_static_fallback(
    monkeypatch: pytest.MonkeyPatch,
    api_models: Any,
) -> None:
    """非法 API 模型列表应分类为协议错误并使用合法静态回退。

    Args:
        monkeypatch: pytest 属性替换工具。
        api_models: provider API 返回的非法值。

    Returns:
        None。
    """
    class InvalidProvider:
        """返回非法模型列表的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> Any:
            """返回参数化非法模型列表。

            Args:
                kwargs: 模型发现参数。

            Returns:
                参数化 API 返回值。
            """
            return api_models

    config = _base_config()
    config["llm"]["default"] = "static-model"
    config["llm_provider"]["stub"]["models"] = ["static-model"]
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: InvalidProvider)
    manager = _manager(config)

    asyncio.run(manager.load_models())

    assert manager.list_models() == ["static-model"]
    assert manager.provider_errors["stub"].kind is LLMErrorKind.RESPONSE_PROTOCOL
    manager.ensure_default_available()


def test_invalid_api_model_list_without_static_models_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 API 模型列表且无静态回退时不得注册模型。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    class InvalidProvider:
        """返回含空模型 ID 的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """返回非法空模型 ID。

            Args:
                kwargs: 模型发现参数。

            Returns:
                含空字符串的列表。
            """
            return [""]

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: InvalidProvider)
    manager = _manager()

    asyncio.run(manager.load_models())

    assert manager.list_models() == []
    assert manager.provider_errors["stub"].kind is LLMErrorKind.RESPONSE_PROTOCOL


@pytest.mark.parametrize("discovery_mode", ["dynamic", "static"])
@pytest.mark.parametrize("provider_order", [("zeta", "alpha"), ("alpha", "zeta")])
def test_cross_provider_model_conflict_is_deterministic_and_atomic(
    monkeypatch: pytest.MonkeyPatch,
    discovery_mode: str,
    provider_order: tuple[str, str],
) -> None:
    """跨 provider 模型冲突应稳定报错且不提交部分状态。

    Args:
        monkeypatch: pytest 属性替换工具。
        discovery_mode: 使用动态发现或静态回退制造冲突。
        provider_order: provider 配置插入顺序。

    Returns:
        None。
    """
    class ConflictProvider:
        """返回冲突模型或触发静态回退的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """按测试模式返回或抛出。

            Args:
                kwargs: 模型发现参数。

            Returns:
                动态模式下返回冲突模型。

            Raises:
                ConnectionError: 静态模式下固定发现失败。
            """
            if discovery_mode == "static":
                raise ConnectionError("connection refused")
            return ["shared-model"]

    config = _base_config()
    config["llm_provider"] = {
        name: {
            "base_url": f"https://{name}.example.test/v1",
            **({"models": ["shared-model"]} if discovery_mode == "static" else {}),
        }
        for name in provider_order
    }
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: ConflictProvider)
    manager = _manager(config)

    with pytest.raises(LLMConfigurationError) as exc_info:
        asyncio.run(manager.load_models())

    message = exc_info.value.info.message
    assert "shared-model" in message
    assert "alpha" in message
    assert "zeta" in message
    assert message.index("alpha") < message.index("zeta")
    assert manager.list_models() == []
    assert manager._cache == {}
    assert manager.provider_errors == {}


def test_failed_reload_preserves_previous_models_errors_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复加载发生冲突时应保留上一次完整状态。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    scripts = {"alpha": ["alpha-model"], "zeta": ["zeta-model"]}

    def provider_class(provider_name: str) -> type:
        """为 provider 名构造读取可变脚本的类型。

        Args:
            provider_name: provider 配置名。

        Returns:
            返回当前脚本模型列表的 provider 类型。
        """
        class ScriptedProvider:
            """返回当前 provider 脚本模型的 provider。"""

            @classmethod
            async def list_models(cls, **kwargs: Any) -> list[str]:
                """返回当前脚本模型列表。

                Args:
                    kwargs: 模型发现参数。

                Returns:
                    当前 provider 的模型列表副本。
                """
                return list(scripts[provider_name])

        return ScriptedProvider

    config = _base_config()
    config["llm_provider"] = {
        name: {"base_url": f"https://{name}.example.test/v1"}
        for name in scripts
    }
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", provider_class)
    manager = _manager(config)
    asyncio.run(manager.load_models())
    cached = object()
    manager._cache["alpha-model"] = cached
    previous_errors = dict(manager.provider_errors)
    scripts["alpha"] = ["shared-model"]
    scripts["zeta"] = ["shared-model"]

    with pytest.raises(LLMConfigurationError):
        asyncio.run(manager.load_models())

    assert manager.list_models() == ["alpha-model", "zeta-model"]
    assert manager._cache == {"alpha-model": cached}
    assert manager.provider_errors == previous_errors


def test_successful_reload_replaces_models_and_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复加载成功时应原子替换模型并清空旧 provider cache。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    api_models = ["old-model"]

    class ReloadProvider:
        """返回可变模型列表的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """返回当前模型列表。

            Args:
                kwargs: 模型发现参数。

            Returns:
                当前模型列表副本。
            """
            return list(api_models)

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: ReloadProvider)
    manager = _manager()
    asyncio.run(manager.load_models())
    manager._cache["old-model"] = object()
    api_models[:] = ["new-model"]

    asyncio.run(manager.load_models())

    assert manager.list_models() == ["new-model"]
    assert manager._cache == {}


def test_reconfigure_keeps_previous_state_when_discovery_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconfigure 发现冲突时应保留上一次的模型表与发现错误。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    scripts = {"alpha": ["alpha-model"], "zeta": ["zeta-model"]}

    def provider_class(provider_name: str) -> type:
        """为 provider 名构造读取可变脚本的类型。

        Args:
            provider_name: provider 配置名。

        Returns:
            返回当前脚本模型列表的 provider 类型。
        """
        class ScriptedProvider:
            """返回当前 provider 脚本模型的 provider。"""

            @classmethod
            async def list_models(cls, **kwargs: Any) -> list[str]:
                """返回当前脚本模型列表。

                Args:
                    kwargs: 模型发现参数。

                Returns:
                    当前 provider 的模型列表副本。
                """
                return list(scripts[provider_name])

        return ScriptedProvider

    config = _base_config()
    config["llm"]["default"] = "alpha-model"
    config["llm_provider"] = {
        name: {"base_url": f"https://{name}.example.test/v1"}
        for name in scripts
    }
    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", provider_class)
    manager = _manager(config)
    asyncio.run(manager.load_models())
    previous_errors = dict(manager.provider_errors)
    scripts["alpha"] = ["shared-model"]
    scripts["zeta"] = ["shared-model"]

    with pytest.raises(LLMConfigurationError):
        asyncio.run(manager.reconfigure())

    assert manager.list_models() == ["alpha-model", "zeta-model"]
    assert manager.provider_errors == previous_errors


def test_reconfigure_keeps_everything_when_llm_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llm.* 配置非法时 reconfigure 必须先于任何状态写入失败。

    校验 __post_init__ 排在 _cache.clear() 之前：此时连 provider 实例缓存都不应丢弃。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    class StubProvider:
        """返回固定模型列表的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """返回固定模型列表。

            Args:
                kwargs: 模型发现参数。

            Returns:
                固定模型列表。
            """
            return ["model-a"]

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: StubProvider)
    config = _base_config()
    manager = _manager(config)
    asyncio.run(manager.load_models())
    cached = object()
    manager._cache["model-a"] = cached
    previous_errors = dict(manager.provider_errors)
    _set_path(config, "llm.concurrency", 0)

    with pytest.raises(LLMConfigurationError):
        asyncio.run(manager.reconfigure())

    assert manager.list_models() == ["model-a"]
    assert manager.provider_errors == previous_errors
    assert manager._cache == {"model-a": cached}


def test_reconfigure_replaces_state_and_clears_cache_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconfigure 成功时应替换模型表并清空旧 provider 实例缓存。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    api_models = ["model-a"]

    class ReloadProvider:
        """返回可变模型列表的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """返回当前模型列表。

            Args:
                kwargs: 模型发现参数。

            Returns:
                当前模型列表副本。
            """
            return list(api_models)

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: ReloadProvider)
    config = _base_config()
    manager = _manager(config)
    asyncio.run(manager.load_models())
    manager._cache["model-a"] = object()
    api_models[:] = ["model-b"]
    _set_path(config, "llm.default", "model-b")

    asyncio.run(manager.reconfigure())

    assert manager.list_models() == ["model-b"]
    assert manager._cache == {}


def test_unknown_discovery_error_logging_never_contains_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知发现异常的安全化堆栈不得记录原始凭据。

    Args:
        monkeypatch: pytest 属性替换工具。
        caplog: pytest 日志捕获器。

    Returns:
        None。
    """
    secret = "arbitrary-unknown-secret"

    class UnknownProvider:
        """固定抛出未知异常的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出含秘密的未知异常。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                RuntimeError: 固定未知异常。
            """
            raise RuntimeError(f"opaque failure {secret}")

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: UnknownProvider)
    manager = _manager()

    with caplog.at_level(logging.ERROR, logger="src.mgr.llm_mgr"):
        asyncio.run(manager.load_models())

    assert manager.provider_errors["stub"].kind is LLMErrorKind.UNKNOWN
    assert secret not in caplog.text
    with pytest.raises(ModelUnavailableError) as exc_info:
        manager.ensure_default_available()
    assert secret not in str(exc_info.value)
    assert "opaque failure" not in str(exc_info.value)


def test_model_discovery_logging_cannot_break_static_fallback_when_traceback_getter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恶意 traceback getter 不得阻止安全记录发现错误和静态回退。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """

    class MaliciousTracebackError(RuntimeError):
        """读取 traceback 时抛出另一个异常的测试错误。"""

        @property
        def __traceback__(self) -> object:
            """拒绝读取异常堆栈。

            Returns:
                本属性不会返回。

            Raises:
                RuntimeError: 每次读取均抛出固定辅助错误。
            """
            raise RuntimeError("traceback getter exploded")

    class FailedProvider:
        """固定抛出带恶意 traceback getter 的未知异常。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出模型发现异常。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                MaliciousTracebackError: 固定未知异常。
            """
            del kwargs
            raise MaliciousTracebackError("opaque provider failure")

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: FailedProvider)
    config = _base_config()
    config["llm"]["default"] = "static-model"
    config["llm_provider"]["stub"]["models"] = ["static-model"]
    manager = _manager(config)

    caught: BaseException | None = None
    try:
        asyncio.run(manager.load_models())
    except BaseException as exc:
        caught = exc

    assert caught is None
    assert manager.list_models() == ["static-model"]
    assert manager.provider_errors["stub"].kind is LLMErrorKind.UNKNOWN


@pytest.mark.parametrize(
    ("unsafe_message", "secret"),
    [
        ("token=tok_live_SECRET", "tok_live_SECRET"),
        ("access_token=access_live_SECRET", "access_live_SECRET"),
        ("refresh_token=refresh_live_SECRET", "refresh_live_SECRET"),
        ("password=password_SECRET", "password_SECRET"),
        ("secret=generic_SECRET", "generic_SECRET"),
        (
            "request failed at https://user:password_SECRET@host.test/path",
            "user:password_SECRET",
        ),
        (
            "request failed at https://host.test/path?token=query_SECRET&other=1",
            "query_SECRET",
        ),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Proxy-Authorization: Custom proxy_SECRET", "proxy_SECRET"),
        ("Token token_scheme_SECRET", "token_scheme_SECRET"),
    ],
)
def test_discovery_logs_and_startup_error_redact_generic_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unsafe_message: str,
    secret: str,
) -> None:
    """已知发现错误的日志和启动错误都不得泄漏通用凭据。

    Args:
        monkeypatch: pytest 属性替换工具。
        caplog: pytest 日志捕获器。
        unsafe_message: 含凭据的供应商结构化消息。
        secret: 输出中不得出现的秘密值。

    Returns:
        None。
    """
    class FailedProvider:
        """返回含通用凭据认证错误的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出参数化认证错误。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                DiscoveryError: 含参数化消息的认证错误。
            """
            raise DiscoveryError(
                unsafe_message,
                status_code=401,
                code="invalid_api_key",
                request_id="req-redaction",
            )

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: FailedProvider)
    manager = _manager()

    with caplog.at_level(logging.WARNING, logger="src.mgr.llm_mgr"):
        asyncio.run(manager.load_models())
    with pytest.raises(ModelUnavailableError) as exc_info:
        manager.ensure_default_available()

    rendered = f"{caplog.text}\n{exc_info.value}"
    assert secret not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(4)],
)
def test_load_models_propagates_control_flow_errors(
    monkeypatch: pytest.MonkeyPatch,
    control_error: BaseException,
) -> None:
    """模型发现不得吞掉任务取消、键盘中断或进程退出。

    Args:
        monkeypatch: pytest 属性替换工具。
        control_error: 待原样传播的控制流异常。

    Returns:
        None。
    """
    class ControlProvider:
        """抛出指定控制流异常的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出外层测试指定的控制流异常。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                BaseException: 指定控制流异常。
            """
            raise control_error

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: ControlProvider)
    manager = _manager()

    with pytest.raises(type(control_error)) as exc_info:
        asyncio.run(manager.load_models())

    assert exc_info.value is control_error


def test_ensure_default_available_requires_exact_model_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模型校验不得接受模糊匹配到的其他模型。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    class SimilarProvider:
        """只返回相似模型名的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """返回唯一相似模型。

            Args:
                kwargs: 模型发现参数。

            Returns:
                与默认模型不精确相同的模型列表。
            """
            return ["model-a-latest"]

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: SimilarProvider)
    manager = _manager()
    asyncio.run(manager.load_models())

    with pytest.raises(ModelUnavailableError) as exc_info:
        manager.ensure_default_available()

    assert "model-a" in str(exc_info.value)
    assert "model-a-latest" in str(exc_info.value)


def test_unavailable_default_reports_safe_discovery_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模型不可用时应包含安全具体原因且不泄漏底层秘密。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        None。
    """
    secret = "sk-startup-super-secret"

    class FailedProvider:
        """发现阶段认证失败的 provider。"""

        @classmethod
        async def list_models(cls, **kwargs: Any) -> list[str]:
            """抛出带请求 ID 的认证错误。

            Args:
                kwargs: 模型发现参数。

            Returns:
                本方法不会返回。

            Raises:
                DiscoveryError: 固定认证错误。
            """
            raise DiscoveryError(
                f"invalid api_key={secret}",
                status_code=401,
                code="invalid_api_key",
                request_id="req-safe",
            )

    monkeypatch.setattr("src.mgr.llm_mgr.get_provider", lambda name: FailedProvider)
    manager = _manager()
    asyncio.run(manager.load_models())

    with pytest.raises(ModelUnavailableError) as exc_info:
        manager.ensure_default_available()

    message = str(exc_info.value)
    assert "authentication" in message
    assert "req-safe" in message
    assert "[REDACTED]" in message
    assert secret not in message


@pytest.mark.parametrize(
    "startup_error",
    [LLMConfigurationError("llm.retry.max_attempts 非法"), ModelUnavailableError("model-a 不可用")],
)
def test_cli_exits_cleanly_for_llm_startup_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    startup_error: Exception,
) -> None:
    """CLI 应干净打印 LLM 启动错误并以非零状态退出。

    Args:
        monkeypatch: pytest 属性替换工具。
        capsys: pytest 标准流捕获器。
        startup_error: 待模拟的启动错误。

    Returns:
        None。
    """
    async def fail_startup(args: Any) -> None:
        """模拟应用启动失败。

        Args:
            args: CLI 参数命名空间。

        Returns:
            本方法不会返回。

        Raises:
            Exception: 参数化的启动错误。
        """
        raise startup_error

    monkeypatch.setattr(main_module, "main", fail_startup)
    monkeypatch.setattr("sys.argv", ["main.py"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.cli()

    stderr = capsys.readouterr().err
    assert exc_info.value.code != 0
    assert "启动失败" in stderr
    assert "Traceback" not in stderr


def test_models_by_provider_groups_and_sorts() -> None:
    """模型 API 应统一按大小写不敏感的 provider/model 完整名称排序。"""
    manager = _manager()
    manager._model_to_provider.update(
        {
            "deepseek-v4-pro": "deepseek",
            "claude-opus-4-8": "anthropic",
            "deepseek-v4-flash": "deepseek",
            "claude-sonnet-5": "anthropic",
            "gpt-5.6-terra": "openai",
            "gpt-5.2-sol": "openai",
            "gpt-5.10-sol": "openai",
            "Alpha-model": "openai",
        }
    )

    assert manager.list_models() == [
        "claude-opus-4-8",
        "claude-sonnet-5",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "Alpha-model",
        "gpt-5.10-sol",
        "gpt-5.2-sol",
        "gpt-5.6-terra",
    ]

    grouped = manager.models_by_provider()

    assert list(grouped.keys()) == ["anthropic", "deepseek", "openai"]
    assert grouped["anthropic"] == ["claude-opus-4-8", "claude-sonnet-5"]
    assert grouped["deepseek"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert grouped["openai"] == [
        "Alpha-model",
        "gpt-5.10-sol",
        "gpt-5.2-sol",
        "gpt-5.6-terra",
    ]


def test_models_by_provider_empty() -> None:
    """空注册表应返回空字典。"""
    manager = _manager()

    assert manager.models_by_provider() == {}
