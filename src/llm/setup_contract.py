"""首次 LLM Provider 配置向导的契约 — 编排层与 TUI 实现共用的叶子模块。

`src/app/provider_setup.py` 负责业务编排（候选构造、模型发现、持久化），
`src/interfaces/tui/provider_setup.py` 是向导的 Textual 实现。两者只共享本模块的
数据结构与回调签名，因此 UI 不必反向依赖 app 层，app 也不必在导入期拉入 Textual。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from src.llm.models import ModelDiscovery


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """内置 Provider 候选（顺序即内置 llm_provider mapping 顺序）。

    api_key 为有效环境中已有的该 Provider 凭据，仅用于向导预填；repr 不含该值。
    """

    name: str
    base_url: str
    requires_key: bool
    api_key: str | None = field(default=None, repr=False)
    models: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SetupResult:
    """向导确认的完整 Provider 配置结果；repr 不含 api_key。

    default_model 写入激活角色的 default 槽位，fast_model 写入 fast 槽位；
    两者可以是同一个模型。
    """

    provider: str
    base_url: str
    api_key: str | None = field(repr=False)
    default_model: str
    fast_model: str


DiscoverFunc = Callable[[ProviderOption, str | None, str], Awaitable[ModelDiscovery]]
