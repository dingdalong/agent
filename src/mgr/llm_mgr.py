from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.llm import get_provider, LLMProvider

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
        llm_cfg = self.config_mgr.get_config("llm")
        self._default_concurrency = llm_cfg.get("concurrency", 5)
        self._default_max_retries = llm_cfg.get("max_retries", 3)
        self._page_token_rate = self.config_mgr.get_config("tool.page_token_rate")

    async def load_models(self) -> None:
        providers_cfg: dict = self.config_mgr.get_config("llm_provider")

        async def _fetch_provider(provider_name: str, provider_cfg: dict) -> tuple[str, list[str]]:
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
                    error_msg = str(e) or type(e).__name__
                    logger.warning("获取 %s 模型列表失败: %s", provider_name, error_msg)
            return provider_name, models

        results = await asyncio.gather(
            *(_fetch_provider(name, cfg) for name, cfg in providers_cfg.items())
        )

        for provider_name, models in results:
            for model in models:
                self._model_to_provider[model] = provider_name
            if models:
                logger.info("从 %s 获取到 %d 个可用模型", provider_name, len(models))

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
