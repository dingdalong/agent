"""Web 参数的本地隐私预检。"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from src.mgr.data_guard import DataGuard


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)")
_CODE_BLOCK = re.compile(r"```|(?:^|\n)\s*(?:class|def|function|SELECT|INSERT)\b")
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:auth|code|credential|key|password|secret|signature|sig|token)(?:$|[_-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WebPrivacyDecision:
    decision: Literal["allow", "deny", "ask"]
    reason: str = ""


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_high_entropy(value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9_-]", "", value)
    return len(compact) >= 24 and _entropy(compact) >= 3.5


def _contains_high_entropy_token(value: str) -> bool:
    return any(
        _looks_high_entropy(match.group(0))
        for match in re.finditer(r"[A-Za-z0-9_-]{24,}", value)
    )


class WebPrivacyGuard:
    def __init__(self, data_guard: DataGuard) -> None:
        self.data_guard = data_guard

    def assess(self, tool_name: str, arguments: Mapping[str, object]) -> WebPrivacyDecision:
        if self.data_guard.contains_secret(arguments):
            return WebPrivacyDecision("deny", "外部工具参数包含敏感数据")
        if tool_name == "web_search":
            query = arguments.get("query")
            return self._assess_search(query if isinstance(query, str) else "")
        if tool_name == "web_fetch":
            url = arguments.get("url")
            return self._assess_url(url if isinstance(url, str) else "")
        return WebPrivacyDecision("allow")

    @staticmethod
    def _assess_search(query: str) -> WebPrivacyDecision:
        if _EMAIL.search(query) or _PHONE.search(query):
            return WebPrivacyDecision("ask", "搜索内容可能包含个人信息")
        if _CODE_BLOCK.search(query) or query.count("\n") >= 4:
            return WebPrivacyDecision("ask", "搜索内容可能包含源代码或专有文本")
        if _contains_high_entropy_token(query):
            return WebPrivacyDecision("ask", "搜索内容包含疑似私有标识符")
        return WebPrivacyDecision("allow")

    @staticmethod
    def _assess_url(url: str) -> WebPrivacyDecision:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return WebPrivacyDecision("deny", "URL 格式无效")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return WebPrivacyDecision("deny", "仅支持带主机名的 http/https URL")
        if parsed.username is not None or parsed.password is not None:
            return WebPrivacyDecision("deny", "URL 不得包含用户凭据")
        expected_port = 80 if parsed.scheme == "http" else 443
        if port not in {None, expected_port}:
            return WebPrivacyDecision("deny", "仅允许 HTTP/HTTPS 标准端口")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(
            (".localhost", ".internal", ".local", ".home", ".lan")
        ):
            return WebPrivacyDecision("deny", "拒绝访问本机、内网或云元数据地址")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return WebPrivacyDecision("deny", "拒绝访问本机、内网或云元数据地址")
        if any(_looks_high_entropy(segment) for segment in parsed.path.split("/")):
            return WebPrivacyDecision("ask", "URL path 包含疑似私有标识符")
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if _SENSITIVE_QUERY_KEY.search(key):
                return WebPrivacyDecision("deny", "URL query 包含认证或签名参数")
            if _looks_high_entropy(value):
                return WebPrivacyDecision("ask", "URL query 包含疑似私有标识符")
        return WebPrivacyDecision("allow")
