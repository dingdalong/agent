"""不可由智能权限或用户覆盖的高置信危险操作检测。"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence

from src.mgr.data_guard import DataGuard
from src.mgr.path_resolver import ResolvedPath
from src.tools.policy import DataFlow, ToolPolicy


_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
_WRAPPERS = {"command", "builtin", "exec", "nohup", "nice", "time", "timeout", "env"}
_PRIVILEGE = {"sudo", "su", "doas", "pkexec"}
_NETWORK = {"curl", "wget", "nc", "ncat", "ssh", "scp", "rsync"}
_INTERPRETERS = {"sh", "bash", "zsh", "fish", "python", "python3", "node", "ruby", "perl", "eval"}
_MUTATING = {"rm", "mv", "cp", "touch", "tee", "sed", "truncate", "install"}
_SENSITIVE_FILE = re.compile(
    r"(?:^|/)(?:\.env[^/]*|credentials(?:\.json)?|id_(?:rsa|ed25519)|[^/]+\.(?:pem|key|p12|pfx))$",
    re.IGNORECASE,
)
_SENSITIVE_ENV = re.compile(
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|COOKIE|AUTH)[A-Za-z0-9_]*\}?",
    re.IGNORECASE,
)
_DOWNLOAD_SUBSTITUTION = re.compile(
    r"(?:\$\(|`)\s*(?:[^\n]*\b)?(?:curl|wget)\b[^\n]*(?:\)|`)\s*(?:\||;)?\s*"
    r"(?:sh|bash|zsh|fish|python\d*|node|ruby|perl|eval)\b",
    re.IGNORECASE,
)
_FORK_BOMB = re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:")


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segments(tokens: Sequence[str]) -> tuple[list[list[str]], list[str]]:
    segments: list[list[str]] = []
    separators: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS or (token and set(token) <= set(";&|()")):
            if current:
                segments.append(current)
                current = []
            separators.append(token)
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments, separators


def _command_name(token: str) -> str:
    return os.path.basename(token.rstrip("/"))


def _strip_wrappers(segment: Sequence[str]) -> list[str]:
    tokens = list(segment)
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    while tokens and _command_name(tokens[0]) in _WRAPPERS:
        wrapper = _command_name(tokens.pop(0))
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if wrapper in {"nice", "timeout"} and option in {"-n", "--adjustment", "-k", "--kill-after", "-s", "--signal"} and tokens:
                tokens.pop(0)
        if wrapper == "timeout" and tokens and re.match(r"^\d+(?:\.\d+)?[smhd]?$", tokens[0]):
            tokens.pop(0)
        while wrapper == "env" and tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
    return tokens


def _has_recursive_flag(tokens: Sequence[str]) -> bool:
    return any(
        token == "--recursive" or (
            token.startswith("-") and not token.startswith("--") and "r" in token.lower()
        )
        for token in tokens
    )


def _system_delete_target(token: str) -> bool:
    normalized = token.rstrip("/") or "/"
    roots = ("/", "~", "$HOME", "${HOME}", "/Users", "/home", "/etc", "/usr", "/var", "/System")
    return any(normalized == root or normalized.startswith(root + "/") for root in roots)


def _is_unscoped_git_clean(tokens: Sequence[str]) -> bool:
    if not tokens or _command_name(tokens[0]) != "git":
        return False
    try:
        clean_index = tokens.index("clean", 1)
    except ValueError:
        return False
    flags: set[str] = set()
    pathspecs: list[str] = []
    after_separator = False
    skip_value = False
    for token in tokens[clean_index + 1:]:
        if skip_value:
            skip_value = False
            continue
        if token == "--":
            after_separator = True
        elif after_separator or not token.startswith("-"):
            pathspecs.append(token)
        elif token in {"-e", "--exclude"}:
            skip_value = True
        elif token.startswith("--"):
            flags.add(token[2:])
        else:
            flags.update(token[1:])
    return {"f", "d", "x"}.issubset(flags) and not pathspecs


def _is_disk_damage(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    command = _command_name(tokens[0])
    if command.startswith("mkfs") or command in {"fdisk", "parted", "wipefs"}:
        return True
    if command == "diskutil" and any(token.lower().startswith("erase") for token in tokens[1:]):
        return True
    return command == "dd" and any(token.startswith("of=/dev/") for token in tokens[1:])


class HardDenyDetector:
    def __init__(self, data_guard: DataGuard) -> None:
        self.data_guard = data_guard

    def check(
        self,
        tool_name: str,
        policy: ToolPolicy,
        arguments: Mapping[str, object],
        paths: Sequence[ResolvedPath] = (),
    ) -> str | None:
        for item in paths:
            if item.role.value in {"write", "destination"} and item.path.name == "trusted_projects.json":
                return "禁止修改项目全局信任库"
        if policy.data_flow is DataFlow.EXTERNAL and self.data_guard.contains_secret(arguments):
            return "外部工具参数包含敏感数据"
        if tool_name != "shell":
            return None
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        if _FORK_BOMB.search(command):
            return "禁止 fork bomb"
        if _DOWNLOAD_SUBSTITUTION.search(command):
            return "禁止下载内容后直接执行"
        try:
            raw_segments, separators = _segments(_tokenize(command))
        except ValueError:
            return "无法安全解析 Shell 命令"
        segments = [_strip_wrappers(segment) for segment in raw_segments]
        segments = [segment for segment in segments if segment]
        commands = [_command_name(segment[0]) for segment in segments]

        if any(name in _PRIVILEGE for name in commands):
            return "禁止提权命令"
        if self.data_guard.contains_secret(command) and any(name in _NETWORK for name in commands):
            return "禁止向网络工具外传敏感数据"
        if _SENSITIVE_ENV.search(command) and any(name in _NETWORK for name in commands):
            return "禁止向网络工具外传敏感环境变量"
        for segment, name in zip(segments, commands, strict=True):
            if name == "rm" and _has_recursive_flag(segment[1:]) and any(
                _system_delete_target(token) for token in segment[1:] if not token.startswith("-")
            ):
                return "禁止对根目录、主目录或系统目录递归删除"
            if _is_disk_damage(segment):
                return "禁止磁盘格式化、分区擦除或块设备写入"
            if name in {"shutdown", "reboot", "halt", "poweroff"}:
                return "禁止关闭或重启系统"
            if _is_unscoped_git_clean(segment):
                return "禁止未限定范围的 git clean -fdx"
            if name in _NETWORK and any(_SENSITIVE_FILE.search(token) for token in segment[1:]):
                return "禁止向网络工具外传凭证文件或环境变量"
            if name in _MUTATING and any("trusted_projects.json" in token for token in segment[1:]):
                return "禁止修改信任库或绕过安全门控"

        if "|" in separators:
            for index, name in enumerate(commands[:-1]):
                if name in {"curl", "wget"} and commands[index + 1] in _INTERPRETERS:
                    return "禁止下载内容后直接执行"
        return None
