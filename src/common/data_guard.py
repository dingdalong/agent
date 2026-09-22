"""敏感数据检测、递归脱敏和安全子进程环境。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values


REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|client[_-]?secret|api[_-]?key|"
    r"aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key)|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|private[_-]?key|(?:^|[_-])token$)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:authorization|cookie|password|passwd|secret|client[_-]?secret|api[_-]?key|token|"
    r"aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key))\b\s*[:=]\s*)"
    r"([^\s,;&]+|\"[^\"]*\"|'[^']*')"
)
_BEARER = re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+\-/=]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PLATFORM_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_AUTH_SCHEMES = {"bearer", "basic"}

_CURL_VALUE_OPTIONS = {
    "-b", "--cookie", "-H", "--header", "-d", "--data", "--data-ascii",
    "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form",
    "--form-string", "-T", "--upload-file", "--json",
}


_MEASURE_PREFIX = re.compile(r"(?:[/／]|(?<![A-Za-z])per\s*|每\s*)$", re.IGNORECASE)
# 计量右侧允许：货币符号、千分位、科学计数、数量级后缀（100M）、单位斜杠（¥5.024/M）
# 与白名单单位词。凭据形状（hunter2、JWT、URL）不在此列，仍按赋值脱敏。
_MEASURE_VALUE = re.compile(
    r"^[¥￥$€]?[+-]?(?:\d[\d,_]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"(?:[KkMmBb万亿])?\s*%?"
    r"(?:/\s*[A-Za-z0-9]+)?(?:元|美元|人民币|tokens?|个|次|万|亿)?$"
)
# Markdown 列表/引用等结构符号不是赋值右侧，命中即跳过。
_STRUCTURAL_VALUE = re.compile(r"^[-*+•|>#]+$")


def _is_measure_key(prefix: str) -> bool:
    """判断 ``key = value`` 里的 key 是否处在计量语境中。

    形如 ``credits/token = 100``、``price per token = 0.000025`` 的写法是速率或
    单价的算式，key 只是一个计量单位名，右侧不是凭据，脱敏会误伤正常文本。
    URL 路径里同样可能出现 ``/token=`` 形状，那属于普通 URL，不算计量语境。

    Args:
        prefix: key 之前已经过脱敏的文本。

    Returns:
        处于计量语境时为 True，调用方跳过本次赋值替换。
    """
    tail = prefix.rsplit(maxsplit=1)[-1] if prefix.strip() else ""
    if "://" in tail:
        return False
    return _MEASURE_PREFIX.search(prefix) is not None


def _is_measure_value(value: str) -> bool:
    """判断赋值右侧是否是计量数值。

    计量语境的豁免必须同时看右侧形状：``token = hunter2`` 或 URL 路径里的
    ``token=abc`` 只是借用了同样的 key，右侧是凭据时必须照常脱敏。

    Args:
        value: ``key = value`` 里匹配到的右侧原文（可能带引号）。

    Returns:
        右侧是纯数值（可带货币符号、千分位、科学计数与白名单单位后缀）时为 True。
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return _MEASURE_VALUE.match(text) is not None


def _is_measure_assignment(text: str, match: re.Match[str]) -> bool:
    """判断一次 ``key = value`` 匹配是否只是计量算式而非凭据赋值。

    检测（``contains_secret``）与脱敏共用同一判定，避免同一段文本在一处被当成
    普通计量、在另一处又被当成秘密，导致外发硬拒绝等误伤。

    Args:
        text: 被扫描的完整文本。
        match: ``_ASSIGNMENT`` 的一次匹配。

    Returns:
        认定为计量算式时为 True。
    """
    return _is_measure_key(text[: match.start(1)]) and _is_measure_value(
        match.group(2),
    )


def _is_structural_value(value: str) -> bool:
    """判断匹配到的右侧是否只是 Markdown 结构符号而非赋值内容。

    ``per token:\n- mimo-v2.6-pro`` 里的 ``-`` 是列表符号，把它当值替换会吃掉
    换行并破坏正文；这类匹配必须整体跳过。

    Args:
        value: ``_ASSIGNMENT`` 匹配到的右侧原文。

    Returns:
        右侧仅由结构符号组成时为 True。
    """
    return _STRUCTURAL_VALUE.match(value) is not None


def _should_redact_assignment(text: str, match: re.Match[str]) -> bool:
    """判断一次赋值匹配是否应按凭据脱敏。

    Args:
        text: 被扫描的完整文本。
        match: ``_ASSIGNMENT`` 的一次匹配。

    Returns:
        右侧是结构符号、认证方案或计量算式时为 False，否则为 True。
    """
    value = match.group(2)
    if _is_structural_value(value):
        return False
    if value.lower() in _AUTH_SCHEMES:
        return False
    return not _is_measure_assignment(text, match)


class DataGuard:
    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self._secrets: set[str] = set()
        if secrets:
            self.register_secrets(secrets)

    def register_secret(self, value: Any) -> None:
        if isinstance(value, str) and len(value) >= 4:
            self._secrets.add(value)

    def clear_secrets(self) -> None:
        self._secrets.clear()

    def register_secrets(self, values: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> None:
        items = values.values() if isinstance(values, Mapping) else values
        for value in items:
            if isinstance(value, Mapping):
                self.register_secrets(value)
            elif isinstance(value, (list, tuple)):
                self.register_secrets(value)
            else:
                self.register_secret(value)

    def register_from_config(self, value: Any, key: str = "") -> None:
        """只登记敏感键下的确切值，避免把普通 URL/路径误当秘密。"""
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                self.register_from_config(child_value, str(child_key))
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self.register_from_config(item, key)
            return
        if _SENSITIVE_KEY.search(key):
            self.register_secret(value)

    def contains_secret(self, value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(
                (_SENSITIVE_KEY.search(str(key)) is not None and bool(item))
                or self.contains_secret(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple, set)):
            return any(self.contains_secret(item) for item in value)
        if not isinstance(value, str):
            return False
        if any(secret in value for secret in self._secrets):
            return True
        if any(
            _should_redact_assignment(value, match)
            for match in _ASSIGNMENT.finditer(value)
        ):
            return True
        return any(
            pattern.search(value)
            for pattern in (_BEARER, _JWT, _PLATFORM_TOKEN, _PRIVATE_KEY)
        )

    def redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: REDACTED if _SENSITIVE_KEY.search(str(key)) and item not in (None, "") else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, set):
            return {self.redact(item) for item in value}
        if isinstance(value, BaseException):
            return f"{type(value).__name__}: {self.redact(str(value))}"
        if not isinstance(value, str):
            return value
        return self._redact_text(value)

    def redact_source(self, text: str) -> str:
        """以等长星号遮盖秘密并保留换行，使文件游标仍对应源文件。"""
        return self._redact_text(text, preserve_positions=True)

    def _redact_text(self, text: str, preserve_positions: bool = False) -> str:
        def replacement(value):
            return ''.join(c if c in '\r\n' else '*' for c in value) if preserve_positions else REDACTED

        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, replacement(secret))
        text = _PRIVATE_KEY.sub(lambda m: replacement(m.group()), text)

        def redact_assignment(match: re.Match[str]) -> str:
            if not _should_redact_assignment(text, match):
                return match.group(0)
            # 只替换右侧值本身，保留 key、分隔符与空白，避免破坏正文排版。
            head = match.group(0)[: match.start(2) - match.start(0)]
            return head + replacement(match.group(2))

        text = _ASSIGNMENT.sub(redact_assignment, text)
        text = _BEARER.sub(lambda m: m.group(1) + replacement(m.group()[len(m.group(1)):]), text)
        for pattern in (_JWT, _PLATFORM_TOKEN):
            text = pattern.sub(lambda m: replacement(m.group()), text)
        if preserve_positions:
            if "://" not in text:
                return text
            return _URL.sub(lambda m: replacement(m.group()) if self._redact_url_match(m) != m.group() else m.group(), text)
        return self.redact_url(text)

    def redact_url(self, text: str) -> str:
        if "://" not in text:
            return text

        return _URL.sub(self._redact_url_match, text)

    def url_summary(self, raw: str) -> str:
        """生成不包含凭据、fragment 或 query value 的 URL 摘要。"""
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "<url>"
        if not parsed.scheme or not parsed.hostname:
            return "<url>"
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return "<url>"
        if port:
            host += f":{port}"
        query_keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        query = "keys=" + ",".join(query_keys[:16]) if query_keys else ""
        if len(query_keys) > 16:
            query += ",..."
        return urlunsplit((parsed.scheme, host, parsed.path or "/", query, ""))

    @staticmethod
    def web_search_summary(query: str) -> str:
        """生成不保留搜索正文的审计摘要。"""
        return f"搜索网页：<query:length={len(query)}>"

    def shell_summary(self, command: str) -> str:
        """生成不含请求数据的 Shell 摘要，供智能权限和人工确认复用。"""
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()

        result: list[str] = []
        redact_next = False
        for token in tokens[:2048]:
            if redact_next:
                result.append(f"<value:length={len(token)}>")
                redact_next = False
                continue
            option, separator, inline_value = token.partition("=")
            if option in _CURL_VALUE_OPTIONS:
                result.append(option)
                if separator:
                    result.append(f"<value:length={len(inline_value)}>")
                else:
                    redact_next = True
                continue
            short_option = next(
                (item for item in ("-H", "-b", "-d", "-F", "-T") if token.startswith(item) and token != item),
                None,
            )
            if short_option:
                result.extend((short_option, f"<value:length={len(token) - len(short_option)}>"))
                continue
            if "://" in token:
                result.append(self._shell_url(token))
                continue
            safe = str(self.redact(token))
            result.append(safe if len(safe) <= 512 else f"<argument:length={len(token)}>")
        summary = shlex.join(result)
        encoded = summary.encode()
        if len(encoded) > 8192:
            summary = encoded[:8192].decode(errors="ignore") + " [truncated]"
        return summary

    @staticmethod
    def _shell_url(raw: str) -> str:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "<url>"
        if not parsed.scheme or not parsed.hostname:
            return "<url>"
        netloc = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return "<url>"
        if port:
            netloc += f":{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def _redact_url_match(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;:!?)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "<url>"
        if not parsed.scheme or not parsed.netloc:
            return match.group(0)
        netloc = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            netloc += f":{port}"
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            query.append((key, REDACTED if _SENSITIVE_KEY.search(key) else self.redact(value)))
        return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment)) + trailing

    def safe_environment(
        self,
        base: Mapping[str, Any],
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        for raw_key, raw_value in base.items():
            key, value = str(raw_key), str(raw_value)
            if _SENSITIVE_KEY.search(key) or self.contains_secret(value):
                continue
            env[key] = value
        for key, value in (extra or {}).items():
            key, value = str(key), str(value)
            env[key] = value
        return env


def register_runtime_secrets(
    data_guard: DataGuard,
    config_mgr: Any,
    global_dir: str | Path,
    workdir: str | Path,
    project_trusted: bool,
) -> None:
    """登记本次配置层实际允许加载的精确秘密。"""
    data_guard.clear_secrets()
    data_guard.register_from_config(config_mgr.get_config("llm_provider"))
    data_guard.register_from_config(config_mgr.load_mcp_servers())
    env_paths = [Path(global_dir) / ".env"]
    if project_trusted:
        env_paths.extend((Path(workdir) / ".env", Path(workdir) / ".agent" / ".env"))
    for path in env_paths:
        try:
            values = dotenv_values(path)
        except (OSError, ValueError):
            continue
        data_guard.register_from_config(values)
