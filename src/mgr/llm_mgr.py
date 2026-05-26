from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.llm import get_provider, LLMProvider

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager
    from src.events import EventBus

logger = logging.getLogger(__name__)


@dataclass
class LLMMgr:
    """LLM 管理器 — 根据模型名返回可用的 LLMProvider 实例。"""

    config_mgr: ConfigManager
    event_bus: EventBus

    _model_to_provider: dict[str, str] = field(init=False, default_factory=dict)
    _cache: dict[str, LLMProvider] = field(init=False, default_factory=dict)
    _default_concurrency: int = field(init=False)
    _default_max_retries: int = field(init=False)
    _page_token_rate: float = field(init=False)

    def __post_init__(self) -> None:
        default_llm_cfg = self.config_mgr.get_config("llm.default")
        self._default_concurrency = default_llm_cfg.get("concurrency", 5)
        self._default_max_retries = default_llm_cfg.get("max_retries", 3)
        self._page_token_rate = self.config_mgr.get_config("tool.page_token_rate")

    async def load_models(self) -> None:
        providers_cfg: dict = self.config_mgr.get_config("llm_provider")
        for provider_name, provider_cfg in providers_cfg.items():
            try:
                ProviderClass = get_provider(provider_name)
                models = await ProviderClass.list_models(
                    api_key=provider_cfg.get("api_key", ""),
                    base_url=provider_cfg["base_url"],
                )
            except Exception as e:
                models = provider_cfg.get("models", [])
                if models:
                    logger.info("从 %s API 获取模型列表失败，使用配置中的模型列表", provider_name)
                else:
                    logger.warning("获取 %s 模型列表失败: %s", provider_name, e)
            for model in models:
                self._model_to_provider[model] = provider_name
            if models:
                logger.info("从 %s 获取到 %d 个可用模型", provider_name, len(models))

    def get(self, model: str | None = None) -> LLMProvider:
        if model is None or model not in self._model_to_provider:
            model = self.config_mgr.get_config("llm.default")["model"]

        cached = self._cache.get(model)
        if cached is not None:
            return cached

        provider_name = self._model_to_provider.get(model)
        if provider_name is None:
            available = ", ".join(sorted(self._model_to_provider)) or "(none)"
            raise ValueError(f"未知的模型: {model!r}，可用模型: {available}")

        instance = self._create_provider(provider_name, model)
        self._cache[model] = instance
        return instance

    def _create_provider(self, provider_name: str, model: str) -> LLMProvider:
        provider_cfg = self.config_mgr.get_config(f"llm_provider.{provider_name}")
        ProviderClass = get_provider(provider_name)
        return ProviderClass(
            api_key=provider_cfg.get("api_key", ""),
            base_url=provider_cfg["base_url"],
            model=model,
            reasoning_effort=provider_cfg.get("reasoning_effort", "max"),
            preserve_thinking=provider_cfg.get("preserve_thinking", False),
            concurrency=self._default_concurrency,
            max_retries=self._default_max_retries,
            context_limit=provider_cfg.get("context_limit", 0),
            page_token_rate=self._page_token_rate,
            event_bus=self.event_bus,
        )

    def list_models(self) -> list[str]:
        return sorted(self._model_to_provider.keys())
