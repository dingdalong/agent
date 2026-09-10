from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.llm import LLMConfigurationError, LLMErrorInfo, LLMStreamResponseError, classify_llm_error, get_provider

logger = logging.getLogger(__name__)
MODEL_DISCOVERY_TIMEOUT_SECONDS = 3.0


def split_model_reference(value: str) -> tuple[str, str]:
    """解析供应商/模型ID，只分隔第一个斜杠。"""
    if not isinstance(value, str):
        raise LLMConfigurationError("模型必须使用非空的 供应商/模型ID")
    provider, separator, model = value.strip().partition("/")
    if not separator or not provider.strip() or not model.strip() or provider != provider.strip():
        raise LLMConfigurationError(f"模型 {value!r} 必须使用 供应商/模型ID")
    return provider, model


@dataclass(frozen=True)
class ModelDiscovery:
    """一次发现的合并候选与可安全展示的错误；不代表调用可用性。"""

    models: list[str]
    error: LLMErrorInfo | None = None


async def discover_models(
    provider_name: str,
    config: Mapping[str, Any],
    *,
    user_agent: str = "",
    timeout: float = MODEL_DISCOVERY_TIMEOUT_SECONDS,
) -> ModelDiscovery:
    """合并配置与在线模型；网络和响应错误只影响候选列表。"""
    configured = normalize_model_list(
        config.get("models", []),
        key=f"llm_provider.{provider_name}.models",
        configuration=True,
    )
    provider_class = get_provider(provider_name)
    try:
        discovered = await asyncio.wait_for(
            provider_class.list_models(
                api_key=config.get("api_key") or ("ollama" if provider_name == "ollama" else ""),
                base_url=config["base_url"],
                timeout=timeout,
                user_agent=user_agent,
            ),
            timeout=timeout,
        )
        models = normalize_model_list(
            discovered, key=f"llm_provider.{provider_name}.list_models", configuration=False
        )
    except Exception as exc:
        info = classify_llm_error(exc)
        logger.warning(
            "模型列表获取失败 provider=%s kind=%s message=%s",
            provider_name, info.kind.value, info.message,
        )
        return ModelDiscovery(sorted(configured, key=lambda model: (model.casefold(), model)), info)
    return ModelDiscovery(sorted(set(configured) | set(models), key=lambda model: (model.casefold(), model)))


def normalize_provider_configs(value: Any) -> dict[str, dict[str, Any]]:
    """校验并复制模型发现所需的 provider 配置。

    Args:
        value: llm_provider 顶层配置值。

    Returns:
        provider 名到已规范化配置的映射。

    Raises:
        LLMConfigurationError: 顶层、provider 项、名称、base_url、models 或
            Anthropic pause_turn 续接上限非法。
    """
    if not isinstance(value, Mapping):
        raise LLMConfigurationError("llm_provider 必须是 mapping")

    normalized: dict[str, dict[str, Any]] = {}
    for provider_name, provider_config in value.items():
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise LLMConfigurationError("llm_provider 的 provider name 必须是非空 str")
        try:
            get_provider(provider_name)
        except ValueError as exc:
            raise LLMConfigurationError(f"llm_provider.{provider_name} 配置了未知 provider 名") from exc
        provider_key = f"llm_provider.{provider_name}"
        if not isinstance(provider_config, Mapping):
            raise LLMConfigurationError(f"{provider_key} 必须是 mapping")
        base_url = provider_config.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise LLMConfigurationError(f"{provider_key}.base_url 必须是非空 str")

        copied_config = dict(provider_config)
        web_mode = provider_config.get("web", "local")
        if web_mode not in {"local", "provider"}:
            raise LLMConfigurationError(
                f"{provider_key}.web 必须是 'local' 或 'provider'"
            )
        copied_config["web"] = web_mode
        copied_config["models"] = normalize_model_list(
            provider_config.get("models", []),
            key=f"{provider_key}.models",
            configuration=True,
        )
        if provider_name == "anthropic":
            pause_limit_key = (
                f"{provider_key}.max_pause_turn_continuations"
            )
            pause_limit = provider_config.get(
                "max_pause_turn_continuations",
                5,
            )
            if (
                isinstance(pause_limit, bool)
                or not isinstance(pause_limit, int)
                or pause_limit < 1
            ):
                raise LLMConfigurationError(
                    f"{pause_limit_key} 必须是非 bool 正整数"
                )
            copied_config["max_pause_turn_continuations"] = pause_limit
        else:
            copied_config["max_pause_turn_continuations"] = 0
        normalized[provider_name] = copied_config
    return normalized


def normalize_model_list(
    value: Any,
    *,
    key: str,
    configuration: bool,
) -> list[str]:
    """校验模型列表并按首次出现顺序去重。

    Args:
        value: 待校验模型列表。
        key: 错误消息使用的精确配置或响应键。
        configuration: 非法值是否作为配置错误抛出。

    Returns:
        仅含非空字符串且已去重的模型 ID 列表。

    Raises:
        LLMConfigurationError: configuration 为 True 且列表非法。
        LLMStreamResponseError: configuration 为 False 且列表非法。
    """
    if not isinstance(value, list):
        _raise_model_list_error(key, "必须是 list", configuration=configuration)

    normalized: list[str] = []
    seen: set[str] = set()
    for index, model in enumerate(value):
        if not isinstance(model, str) or not model.strip():
            _raise_model_list_error(
                f"{key}[{index}]",
                "必须是非空 str",
                configuration=configuration,
            )
        if model not in seen:
            seen.add(model)
            normalized.append(model)
    return normalized


def _raise_model_list_error(
    key: str,
    detail: str,
    *,
    configuration: bool,
) -> None:
    """按来源抛出模型列表配置或响应协议错误。

    Args:
        key: 非法值的精确键名。
        detail: 非法值约束说明。
        configuration: 是否抛出配置错误。

    Returns:
        本函数不会返回。

    Raises:
        LLMConfigurationError: configuration 为 True。
        LLMStreamResponseError: configuration 为 False。
    """
    message = f"{key} {detail}"
    if configuration:
        raise LLMConfigurationError(message)
    raise LLMStreamResponseError(message, code="invalid_response")
