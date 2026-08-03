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
_CURL_VALUE_OPTIONS = {
    "-b", "--cookie", "-H", "--header", "-d", "--data", "--data-ascii",
    "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form",
    "--form-string", "-T", "--upload-file", "--json",
}


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
        return any(pattern.search(value) for pattern in (_ASSIGNMENT, _BEARER, _JWT, _PLATFORM_TOKEN, _PRIVATE_KEY))

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
        text = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        text = _PRIVATE_KEY.sub(REDACTED, text)
        text = _ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, text)
        text = _BEARER.sub(lambda match: match.group(1) + REDACTED, text)
        text = _JWT.sub(REDACTED, text)
        text = _PLATFORM_TOKEN.sub(REDACTED, text)
        return self.redact_url(text)

    def redact_url(self, text: str) -> str:
        if "://" not in text:
            return text

        return _URL.sub(self._redact_url_match, text)

    def shell_summary(self, command: str) -> str:
        """生成不含请求数据的 Shell 摘要，供判官和人工确认复用。"""
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
        if parsed.port:
            netloc += f":{parsed.port}"
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
            return match.group(0)
        if not parsed.scheme or not parsed.netloc:
            return match.group(0)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
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
