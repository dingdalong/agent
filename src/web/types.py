"""Web 后端之间共享的规范化结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


class NativeWebCapabilityError(RuntimeError):
    """当前 provider 或模型不支持请求的原生 Web 能力。"""


@dataclass(frozen=True, slots=True)
class WebSource:
    url: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    summary: str
    sources: tuple[WebSource, ...] = ()
    token_usage: dict[str, int | None] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WebFetchResponse:
    requested_url: str
    final_url: str
    content: str
    title: str = ""
    status: int | None = None
    content_type: str = ""
    retrieved_at: float | None = None
    token_usage: dict[str, int | None] | None = field(default=None, repr=False)
