from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.llm import (
    LLMConfigurationError,
    LLMErrorInfo,
    LLMErrorKind,
    LLMProvider,
    LLMStreamResponseError,
    RetryConfig,
    classify_llm_error,
    get_provider,
)
from src.llm.errors import safe_exception_traceback

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager
    from src.events import EventBus

logger = logging.getLogger(__name__)

# Claude Code 兼容映射：将 Claude Code 的模型别名映射到本项目的通用别名
_CLAUDECODE_ALIASES: dict[str, str] = {
    "opus": "best",
    "sonnet": "default",
    "haiku": "fast",
}


class ModelUnavailableError(Exception):
    """默认模型在 load_models() 之后仍不可用。

    用于启动期前置校验：携带面向用户的可操作提示，由入口层（main.cli）捕获后
    清晰退出，避免在创建 Agent 时抛出深层 ValueError 堆栈。
    """


@dataclass
class LLMMgr:
    """LLM 管理器 — 根据模型名返回可用的 LLMProvider 实例。"""

    config_mgr: ConfigManager
    event_bus: EventBus

    _model_to_provider: dict[str, str] = field(init=False, default_factory=dict)
    _cache: dict[str, LLMProvider] = field(init=False, default_factory=dict)
    _provider_web_mode: dict[str, str] = field(init=False, default_factory=dict)
    provider_errors: dict[str, LLMErrorInfo] = field(init=False, default_factory=dict)
    _default_concurrency: int = field(init=False)
    _timeout_seconds: float = field(init=False)
    _retry_config: RetryConfig = field(init=False)
    _page_token_rate: float = field(init=False)
    _user_agent: str = field(init=False)

    def __post_init__(self) -> None:
        """解析并校验 LLM 运行配置。

        Returns:
            None。

        Raises:
            LLMConfigurationError: 任一 LLM 配置值类型或范围非法。
        """
        llm_cfg = self.config_mgr.get_config("llm")
        if not isinstance(llm_cfg, Mapping):
            raise LLMConfigurationError("llm 必须是 mapping")
        if "max_retries" in llm_cfg:
            raise LLMConfigurationError(
                "llm.max_retries 已不受支持，请改用 llm.retry.max_attempts"
            )

        concurrency = llm_cfg.get("concurrency", 5)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise LLMConfigurationError("llm.concurrency 必须是非 bool 正整数")

        timeout_seconds = _positive_finite_number(
            llm_cfg.get("timeout_seconds", 120.0),
            key="llm.timeout_seconds",
        )
        retry_cfg = llm_cfg.get("retry", {})
        if not isinstance(retry_cfg, Mapping):
            raise LLMConfigurationError("llm.retry 必须是 mapping")

        max_attempts = retry_cfg.get("max_attempts", 3)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise LLMConfigurationError(
                "llm.retry.max_attempts 必须是非 bool 且大于等于 1 的整数"
            )
        base_delay_seconds = _positive_finite_number(
            retry_cfg.get("base_delay_seconds", 2.0),
            key="llm.retry.base_delay_seconds",
        )
        max_delay_seconds = _positive_finite_number(
            retry_cfg.get("max_delay_seconds", 60.0),
            key="llm.retry.max_delay_seconds",
        )
        if max_delay_seconds < base_delay_seconds:
            raise LLMConfigurationError(
                "llm.retry.max_delay_seconds 必须大于等于 "
                "llm.retry.base_delay_seconds"
            )

        self._default_concurrency = concurrency
        self._timeout_seconds = timeout_seconds
        self._retry_config = RetryConfig(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        self._page_token_rate = self.config_mgr.get_config("tool.page_token_rate")
        self._user_agent = llm_cfg.get("user_agent", "")

    async def load_models(self) -> None:
        """并发发现 provider 模型并记录安全分类后的失败原因。

        API 发现失败且 provider 配置了非空静态 models 时使用静态列表；
        否则该 provider 不注册任何模型。

        Returns:
            None。

        Raises:
            LLMConfigurationError: provider 配置、名称或跨 provider 模型归属非法。
            asyncio.CancelledError: 模型发现任务被取消时原样传播。
            KeyboardInterrupt: 收到键盘中断时原样传播。
            SystemExit: 进程退出时原样传播。
        """
        providers_cfg = _normalize_provider_configs(
            self.config_mgr.get_config("llm_provider")
        )
        provider_classes: dict[str, type[LLMProvider]] = {}
        for provider_name in providers_cfg:
            try:
                provider_classes[provider_name] = get_provider(provider_name)
            except ValueError as exc:
                raise LLMConfigurationError(
                    f"llm_provider.{provider_name} 配置了未知 provider 名"
                ) from exc

        async def _fetch_provider(
            provider_name: str,
            provider_cfg: Mapping[str, Any],
        ) -> tuple[str, list[str], LLMErrorInfo | None]:
            """发现单个 provider 模型并应用静态回退。

            Args:
                provider_name: provider 配置名。
                provider_cfg: 单个 provider 配置映射。

            Returns:
                provider 名、最终可注册模型列表和可选发现错误。

            Raises:
                asyncio.CancelledError: 模型发现任务被取消时原样传播。
                KeyboardInterrupt: 收到键盘中断时原样传播。
                SystemExit: 进程退出时原样传播。
            """
            ProviderClass = provider_classes[provider_name]
            try:
                discovered_models = await ProviderClass.list_models(
                    api_key=provider_cfg.get("api_key", ""),
                    base_url=provider_cfg["base_url"],
                    timeout=self._timeout_seconds,
                    user_agent=self._user_agent,
                )
                models = _normalize_model_list(
                    discovered_models,
                    key=f"llm_provider.{provider_name}.list_models",
                    configuration=False,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                info = classify_llm_error(exc)
                self._log_model_discovery_failure(
                    provider_name=provider_name,
                    info=info,
                    exc=exc,
                )
                models = provider_cfg["models"]
                if models:
                    logger.info(
                        "模型发现失败后使用静态列表 provider=%s model_count=%d",
                        provider_name,
                        len(models),
                    )
                return provider_name, models, info
            return provider_name, models, None

        results = await asyncio.gather(
            *(_fetch_provider(name, cfg) for name, cfg in providers_cfg.items())
        )

        next_model_to_provider: dict[str, str] = {}
        next_provider_errors: dict[str, LLMErrorInfo] = {}
        for provider_name, models, info in results:
            if info is not None:
                next_provider_errors[provider_name] = info
            for model in models:
                previous_provider = next_model_to_provider.get(model)
                if previous_provider is not None and previous_provider != provider_name:
                    first_provider, second_provider = sorted(
                        (previous_provider, provider_name)
                    )
                    raise LLMConfigurationError(
                        f"模型 {model!r} 同时归属于 provider "
                        f"{first_provider!r} 和 {second_provider!r}"
                    )
                next_model_to_provider[model] = provider_name
            if models:
                logger.info("从 %s 获取到 %d 个可用模型", provider_name, len(models))

        self._model_to_provider = next_model_to_provider
        self.provider_errors = next_provider_errors
        self._provider_web_mode = {
            name: cfg["web"] for name, cfg in providers_cfg.items()
        }
        self._cache.clear()

    async def reconfigure(self) -> None:
        """重新读取运行配置并重建 provider/model 缓存。"""
        self._model_to_provider.clear()
        self.provider_errors.clear()
        self._cache.clear()
        self.__post_init__()
        await self.load_models()
        self.ensure_default_available()

    def _log_model_discovery_failure(
        self,
        *,
        provider_name: str,
        info: LLMErrorInfo,
        exc: Exception,
    ) -> None:
        """记录不含响应体和凭据的模型发现失败信息。

        Args:
            provider_name: 失败的 provider 配置名。
            info: 已分类并安全化的错误信息。
            exc: 原始异常，仅未知类别借用其 traceback 帧。

        Returns:
            None。
        """
        fields = (
            "模型发现失败 provider=%s kind=%s retryable=%s status=%s "
            "provider_code=%s request_id=%s exception_type=%s message=%r"
        )
        args = (
            provider_name,
            info.kind.value,
            info.retryable,
            info.status_code,
            info.provider_code,
            info.request_id,
            info.original_exception_type,
            info.message,
        )
        if info.kind is not LLMErrorKind.UNKNOWN:
            logger.warning(fields, *args)
            return
        traceback = safe_exception_traceback(exc)
        safe_exception = RuntimeError(info.message).with_traceback(traceback)
        logger.error(
            fields,
            *args,
            exc_info=(
                type(safe_exception),
                safe_exception,
                traceback,
            ),
        )

    def resolve_model(self, model: str | None = None) -> str:
        """解析模型名或别名为真实模型标识符。

        解析顺序：None → "default" → Claude Code 兼容映射 → 配置别名 → 精确匹配 → 模糊匹配 → 回退默认。

        Args:
            model: 模型名、别名或 None。

        Returns:
            真实模型标识符。
        """
        if model is None:
            model = "default"

        # Claude Code 兼容映射（opus→best 等）
        model = _CLAUDECODE_ALIASES.get(model, model)

        # 别名解析（default/best/fast → 实际模型 ID，best/fast 不存在时回退 default）
        llm_cfg = self.config_mgr.get_config("llm")
        default_model = llm_cfg["default"]
        aliases = {
            "default": default_model,
            "best": llm_cfg.get("best", default_model),
            "fast": llm_cfg.get("fast", default_model),
        }
        if model in aliases:
            model = aliases[model]

        if model in self._model_to_provider:
            return model
        candidates = [m for m in self._model_to_provider if model in m]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return min(candidates, key=len)
        logger.warning("未找到匹配模型 %r，使用默认模型", model)
        return default_model

    def ensure_default_available(self) -> None:
        """精确校验配置的默认模型已成功加载。

        Raises:
            ModelUnavailableError: 默认模型不在已加载的可用模型集合中。
        """
        default_model = self.config_mgr.get_config("llm")["default"]
        if default_model in self._model_to_provider:
            return
        available = ", ".join(sorted(self._model_to_provider)) or "(无)"
        failure_details = "\n".join(
            _format_provider_error(provider_name, info)
            for provider_name, info in sorted(self.provider_errors.items())
        )
        failure_note = (
            f"\n模型发现失败：\n{failure_details}"
            if failure_details
            else ""
        )
        raise ModelUnavailableError(
            f"默认模型 {default_model!r} 不可用，无法启动。\n"
            f"当前可用模型：{available}{failure_note}\n"
            f"请排查：\n"
            f"  1. 该模型所属 provider 的认证/连通性 —— 检查 .env 中的 "
            f"*_API_KEY / *_API_URL 是否被正确加载"
            f"（.env 需位于 ~/.agent/.env、仓库根 .env 或 .agent/.env）。\n"
            f"  2. src/config.yaml 中 llm.default 是否指向一个可用模型。"
        )

    def get(self, model: str | None = None) -> LLMProvider:
        resolved = self.resolve_model(model)

        cached = self._cache.get(resolved)
        if cached is not None:
            return cached

        provider_name = self._model_to_provider.get(resolved)
        if provider_name is None:
            available = ", ".join(sorted(self._model_to_provider)) or "(none)"
            raise ValueError(f"未知的模型: {model!r} (resolved: {resolved!r})，可用模型: {available}")

        instance = self._create_provider(provider_name, resolved)
        self._cache[resolved] = instance
        return instance

    def provider_name_for_model(self, model: str) -> str:
        """返回精确或别名模型所属的 provider 配置名。"""
        resolved = self.resolve_model(model)
        provider_name = self._model_to_provider.get(resolved)
        if provider_name is None:
            raise ValueError(f"未知模型所属 provider：{model!r}")
        return provider_name

    def web_mode_for_model(self, model: str) -> str:
        """返回模型所属 provider 的统一 Web 路由模式。"""
        provider_name = self.provider_name_for_model(model)
        return self._provider_web_mode.get(provider_name, "local")

    def _create_provider(self, provider_name: str, model: str) -> LLMProvider:
        """用已校验的统一运行参数创建 provider。

        Args:
            provider_name: provider 配置名。
            model: 精确模型 ID。

        Returns:
            初始化完成的 provider 实例。
        """
        provider_cfg = _normalize_provider_configs(
            self.config_mgr.get_config("llm_provider")
        )[provider_name]
        ProviderClass = get_provider(provider_name)
        return ProviderClass(
            api_key=provider_cfg.get("api_key", ""),
            base_url=provider_cfg["base_url"],
            model=model,
            max_pause_turn_continuations=provider_cfg[
                "max_pause_turn_continuations"
            ],
            reasoning_effort=provider_cfg.get("reasoning_effort", "max"),
            preserve_thinking=provider_cfg.get("preserve_thinking", False),
            concurrency=self._default_concurrency,
            timeout=self._timeout_seconds,
            max_attempts=self._retry_config.max_attempts,
            base_delay_seconds=self._retry_config.base_delay_seconds,
            max_delay_seconds=self._retry_config.max_delay_seconds,
            context_limit=provider_cfg.get("context_limit", 0),
            page_token_rate=self._page_token_rate,
            event_bus=self.event_bus,
            user_agent=self._user_agent,
        )

    def list_models(self) -> list[str]:
        return sorted(self._model_to_provider.keys())

    def models_by_provider(self) -> dict[str, list[str]]:
        """按 provider 配置名分组返回已注册模型。

        Returns:
            以 provider 配置名为键（升序）、对应模型 ID 升序列表为值的字典。
        """
        grouped: dict[str, list[str]] = {}
        for model, provider in self._model_to_provider.items():
            grouped.setdefault(provider, []).append(model)
        return {provider: sorted(models) for provider, models in sorted(grouped.items())}


def _positive_finite_number(value: Any, *, key: str) -> float:
    """把配置值校验并转换为有限正浮点数。

    Args:
        value: 待校验配置值。
        key: 错误消息使用的完整配置键。

    Returns:
        转换后的有限正浮点数。

    Raises:
        LLMConfigurationError: 值为 bool、非数字、非有限数或非正数。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMConfigurationError(f"{key} 必须是非 bool 的有限正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise LLMConfigurationError(f"{key} 必须是非 bool 的有限正数")
    return number


def _normalize_provider_configs(value: Any) -> dict[str, dict[str, Any]]:
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
        copied_config["models"] = _normalize_model_list(
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


def _normalize_model_list(
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


def _format_provider_error(provider_name: str, info: LLMErrorInfo) -> str:
    """格式化可安全展示的 provider 发现失败摘要。

    Args:
        provider_name: provider 配置名。
        info: 已分类并安全化的错误信息。

    Returns:
        包含类别、消息和可选请求 ID 的单行摘要。
    """
    request_note = f", request_id={info.request_id}" if info.request_id else ""
    return (
        f"  - {provider_name}: kind={info.kind.value}, "
        f"message={info.message}{request_note}"
    )
