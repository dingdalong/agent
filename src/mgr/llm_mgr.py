from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.llm import (
    LLMConfigurationError,
    LLMErrorInfo,
    LLMProvider,
    RetryConfig,
    get_provider,
)
from src.llm.models import discover_models, normalize_provider_configs, split_model_reference
from src.mgr.role_mgr import (
    DEFAULT_ROLE,
    format_role_config_key,
    role_model_yaml_example,
)

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager
    from src.mgr.role_mgr import RoleMgr
    from src.events import EventBus

# Claude Code 兼容映射：将 Claude Code 的模型别名映射到本项目的角色模型槽位
_CLAUDECODE_ALIASES: dict[str, str] = {
    "opus": "default",
    "sonnet": "default",
    "haiku": "fast",
}

# 角色模型槽位名 — 每个角色都必须同时配置这两个槽位，无内置兜底值。
_SLOT_NAMES = frozenset({"default", "fast"})

# 子 agent manifest 的 model 字段允许出现的全部别名。
MODEL_ALIASES = _SLOT_NAMES | set(_CLAUDECODE_ALIASES)


def _alphabetical_key(value: str) -> tuple[str, str]:
    """生成大小写不敏感且结果稳定的 A-Z 排序键。"""
    return value.casefold(), value


@dataclass
class LLMMgr:
    """LLM 管理器 — 候选列表与供应商请求路由独立管理。"""

    config_mgr: ConfigManager
    role_mgr: RoleMgr
    event_bus: EventBus

    _providers: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)
    _discovered_models: dict[str, list[str]] = field(init=False, default_factory=dict)
    _cache: dict[str, LLMProvider] = field(init=False, default_factory=dict)
    provider_errors: dict[str, LLMErrorInfo] = field(init=False, default_factory=dict)
    _default_concurrency: int = field(init=False)
    _request_timeout_seconds: float = field(init=False)
    _retry_config: RetryConfig = field(init=False)
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

        max_attempts = retry_cfg.get("max_attempts", 10)
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
            retry_cfg.get("max_delay_seconds", 300.0),
            key="llm.retry.max_delay_seconds",
        )
        if max_delay_seconds < base_delay_seconds:
            raise LLMConfigurationError(
                "llm.retry.max_delay_seconds 必须大于等于 "
                "llm.retry.base_delay_seconds"
            )

        providers = normalize_provider_configs(self.config_mgr.get_config("llm_provider"))
        self._default_concurrency = concurrency
        self._request_timeout_seconds = timeout_seconds
        self._retry_config = RetryConfig(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        self._user_agent = llm_cfg.get("user_agent", "")
        self._providers = providers

    async def refresh_models(self) -> None:
        """显式刷新候选快照，不改变所选模型或客户端缓存。"""
        providers = self._providers
        results = await asyncio.gather(
            *(discover_models(name, config, user_agent=self._user_agent) for name, config in providers.items())
        )
        if providers is not self._providers:
            return
        self._discovered_models = {
            name: result.models for name, result in zip(providers, results)
        }
        self.provider_errors = {
            name: result.error for name, result in zip(providers, results) if result.error is not None
        }

    def reconfigure(self) -> None:
        """重读本地配置并清空旧端点的缓存，不执行在线发现。"""
        self.__post_init__()
        self._cache.clear()
        self._discovered_models.clear()
        self.provider_errors.clear()

    @property
    def _active_role_name(self) -> str:
        """返回槽位配置所属的激活角色名。

        Returns:
            RoleMgr 给出的角色名；无激活角色时为 DEFAULT_ROLE。
        """
        return self.role_mgr.role_name or DEFAULT_ROLE

    def _role_model_slots(self) -> dict[str, str]:
        """读取激活角色的 default/fast 双槽位模型配置。

        每次调用都现读配置，因此 /models 切换并 reload 后立即生效。

        Returns:
            以槽位名为键、完整模型引用为值的映射，必含 default 与 fast。

        Raises:
            LLMConfigurationError: 槽位配置缺失或格式非法。
        """
        role_name = self._active_role_name
        key = format_role_config_key(role_name, "model")
        try:
            raw = self.config_mgr.get_config_parts(("role", role_name, "model"))
        except KeyError:
            raise LLMConfigurationError(
                self._slot_config_help(role_name, f"{key} 未配置")
            ) from None

        if not isinstance(raw, Mapping):
            raise LLMConfigurationError(
                self._slot_config_help(role_name, f"{key} 必须是 mapping")
            )

        slots: dict[str, str] = {}
        for slot in ("default", "fast"):
            if slot not in raw:
                raise LLMConfigurationError(
                    self._slot_config_help(
                        role_name,
                        f"{format_role_config_key(role_name, 'model', slot)} 未配置",
                    )
                )
            value = raw[slot]
            if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
                raise LLMConfigurationError(
                    self._slot_config_help(
                        role_name,
                        f"{format_role_config_key(role_name, 'model', slot)} 必须是非空 str",
                    )
                )
            provider_name, model_id = split_model_reference(value)
            if provider_name not in self._providers:
                raise LLMConfigurationError(f"模型供应商 {provider_name!r} 未配置")
            slots[slot] = f"{provider_name}/{model_id}"
        return slots

    def _slot_config_help(self, role_name: str, detail: str) -> str:
        """拼装含完整键名、YAML 样例与目标配置文件路径的槽位配置错误消息。

        LLMConfigurationError 会把消息压成单行并限长，故 YAML 样例使用流式写法，
        保证折行后仍可直接粘贴。

        Args:
            role_name: 激活角色名。
            detail: 具体错误原因，已包含完整配置键名。

        Returns:
            面向用户的可操作提示。
        """
        default_key = format_role_config_key(role_name, "model", "default")
        fast_key = format_role_config_key(role_name, "model", "fast")
        example = role_model_yaml_example(role_name)
        base = (
            f"{detail}。必填 {default_key} 与 {fast_key}，无内置兜底值。"
            f"YAML 样例：{example}。"
            f"请写入全局 {self.config_mgr.global_config_path} "
            f"或项目 {self.config_mgr.project_config_path}；"
            f"项目未信任时项目层配置会被忽略，此时只能改全局配置。"
        )
        return base

    def resolve_model(self, model: str | None = None) -> str:
        """将槽位或显式引用解析为供应商/模型ID，不查询候选列表。"""
        if model is not None and not isinstance(model, str):
            raise LLMConfigurationError("模型必须是字符串")
        requested = (model or "").strip() or "default"
        name = _CLAUDECODE_ALIASES.get(requested, requested)
        if name in _SLOT_NAMES:
            name = self._role_model_slots()[name]
        provider_name, model_id = split_model_reference(name)
        if provider_name not in self._providers:
            raise LLMConfigurationError(f"模型供应商 {provider_name!r} 未配置")
        return f"{provider_name}/{model_id}"

    def get(self, model: str | None = None) -> LLMProvider:
        resolved = self.resolve_model(model)
        if resolved not in self._cache:
            provider_name, model_id = split_model_reference(resolved)
            self._cache[resolved] = self._create_provider(provider_name, model_id)
        return self._cache[resolved]

    def web_mode_for_provider(self, provider_name: str) -> str:
        """按明确的供应商身份读取 Web 路由配置。"""
        return self._providers[provider_name]["web"]

    def _create_provider(self, provider_name: str, model: str) -> LLMProvider:
        """用已校验的统一运行参数创建 provider。

        Args:
            provider_name: provider 配置名。
            model: 精确模型 ID。

        Returns:
            初始化完成的 provider 实例。
        """
        provider_cfg = self._providers[provider_name]
        ProviderClass = get_provider(provider_name)
        return ProviderClass(
            api_key=provider_cfg.get("api_key", ""),
            base_url=provider_cfg["base_url"],
            model=model,
            provider_name=provider_name,
            max_pause_turn_continuations=provider_cfg[
                "max_pause_turn_continuations"
            ],
            preserve_thinking=provider_cfg.get("preserve_thinking", False),
            concurrency=self._default_concurrency,
            timeout=self._request_timeout_seconds,
            max_attempts=self._retry_config.max_attempts,
            base_delay_seconds=self._retry_config.base_delay_seconds,
            max_delay_seconds=self._retry_config.max_delay_seconds,
            context_limit=provider_cfg.get("context_limit", 0),
            event_bus=self.event_bus,
            user_agent=self._user_agent,
        )

    def list_models(self) -> list[str]:
        """返回配置、在线发现和已选模型的完整引用并集。"""
        models = {
            f"{name}/{model}"
            for name, config in self._providers.items()
            for model in [*config["models"], *self._discovered_models.get(name, [])]
        }
        models.update(self._role_model_slots().values())
        return sorted(models, key=_alphabetical_key)

    def models_by_provider(self) -> dict[str, list[str]]:
        """按供应商分组返回候选的原始模型 ID。"""
        grouped: dict[str, list[str]] = {}
        for reference in self.list_models():
            provider, model = split_model_reference(reference)
            grouped.setdefault(provider, []).append(model)
        return grouped


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
