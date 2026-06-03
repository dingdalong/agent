from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
import shlex
from typing import Any
from src.mgr.permission_mgr import PermissionCheckResult, PermissionContext
from src.tools.decorator import ToolPermission, tool
from pydantic import BaseModel, Field


class Shell(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=300, description="超时时间（秒）")


SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "\n"}
SHELL_WRAPPERS = {"command", "builtin", "exec", "nohup", "nice"}
SHELL_PRIVILEGE_COMMANDS = {"sudo", "su", "doas"}
SHELL_SCRIPT_INTERPRETERS = {"sh", "bash", "zsh", "fish", "dash"}
SHELL_DISK_ERASE_COMMANDS = {"diskutil"}


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _split_unquoted_newlines(command: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            current.append(char)
            continue
        if char == "\n" and quote is None:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _shell_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            if token == "|":
                segments.append([token])
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_shell_wrappers(segment: list[str]) -> list[str]:
    while segment and segment[0] in SHELL_WRAPPERS:
        segment = segment[1:]
    if segment and segment[0] == "env":
        segment = segment[1:]
        while segment and (
            "=" in segment[0] and not segment[0].startswith("-")
            or segment[0] in {"-i", "-"}
        ):
            segment = segment[1:]
    if segment and segment[0] == "timeout":
        segment = segment[1:]
        while segment and segment[0].startswith("-"):
            segment = segment[1:]
        if segment:
            segment = segment[1:]
    return segment


def _command_name(command: str) -> str:
    return PurePosixPath(command).name


def _is_recursive_rm(segment: list[str]) -> bool:
    command = _command_name(segment[0]) if segment else ""
    if command != "rm":
        return False
    for token in segment[1:]:
        if token == "--":
            return False
        if token == "--recursive":
            return True
        if token.startswith("-") and not token.startswith("--") and "r" in token.lower():
            return True
    return False


def _has_recursive_flag(segment: list[str]) -> bool:
    for token in segment[1:]:
        if token == "--":
            return False
        if token in {"-R", "-r", "--recursive"}:
            return True
        if token.startswith("-") and not token.startswith("--") and "r" in token.lower():
            return True
    return False


def _is_dangerous_dd(segment: list[str]) -> bool:
    if not segment or _command_name(segment[0]) != "dd":
        return False
    return any(token.startswith("of=/dev/") for token in segment[1:])


def _is_dangerous_find(segment: list[str]) -> bool:
    if not segment or _command_name(segment[0]) != "find":
        return False
    if "-delete" in segment:
        return True
    if "-exec" not in segment:
        return False
    exec_index = segment.index("-exec")
    exec_segment = [token for token in segment[exec_index + 1:] if token not in {"{}", "\\;", ";", "+"}]
    return _is_shell_deny_segment(exec_segment)


def _is_dangerous_git(segment: list[str]) -> bool:
    if len(segment) < 3 or _command_name(segment[0]) != "git" or segment[1] != "clean":
        return False
    options = [token for token in segment[2:] if token.startswith("-")]
    if any(token in {"--dry-run", "-n"} or token.startswith("-") and "n" in token for token in options):
        return False
    return any(token.startswith("-") and "f" in token and "d" in token for token in options)


def _shell_c_command(segment: list[str]) -> str | None:
    command = _command_name(segment[0]) if segment else ""
    if command not in SHELL_SCRIPT_INTERPRETERS:
        return None
    for index, token in enumerate(segment[1:], start=1):
        if token == "--":
            return None
        if token.startswith("-") and "c" in token and index + 1 < len(segment):
            return segment[index + 1]
    return None


def _is_shell_deny_segment(segment: list[str]) -> bool:
    segment = _strip_shell_wrappers(segment)
    if not segment:
        return False
    command = _command_name(segment[0])
    if command in SHELL_PRIVILEGE_COMMANDS:
        return True
    if _is_recursive_rm(segment):
        return True
    if command in {"chmod", "chown"} and _has_recursive_flag(segment):
        return True
    if _is_dangerous_dd(segment):
        return True
    if command.startswith("mkfs"):
        return True
    if command in SHELL_DISK_ERASE_COMMANDS and any(
        "erase" in token.lower() for token in segment[1:]
    ):
        return True
    if _is_dangerous_git(segment):
        return True
    if _is_dangerous_find(segment):
        return True
    shell_command = _shell_c_command(segment)
    if shell_command is not None and _is_dangerous_command(shell_command):
        return True
    return False


def _is_dangerous_command(command: str) -> bool:
    """判断 shell 命令字符串是否包含危险操作。

    Args:
        command: 待检查的 shell 命令。

    Returns:
        True 表示命令危险，应拒绝执行。
    """
    if "`" in command:
        return True
    try:
        segments: list[list[str]] = []
        for part in _split_unquoted_newlines(command):
            segments.extend(_shell_segments(_shell_tokens(part)))
    except ValueError:
        return True
    for index, segment in enumerate(segments):
        if _is_shell_deny_segment(segment):
            return True
        if (
            segment == ["|"]
            and index > 0
            and index + 1 < len(segments)
            and segments[index + 1]
            and segments[index + 1][0] in SHELL_SCRIPT_INTERPRETERS
        ):
            previous = _strip_shell_wrappers(segments[index - 1])
            if previous and _command_name(previous[0]) in {"curl", "wget"}:
                return True
    return False


def check_shell_permissions(values: dict[str, Any], ctx: PermissionContext) -> PermissionCheckResult:
    """shell 安全检查：检测危险命令并阻止执行。规则匹配由 check() 统一处理。

    Args:
        values: 工具调用参数，需包含 "command" 字段。
        ctx: 权限上下文。

    Returns:
        PermissionCheckResult 权限检查结果。
    """
    command = values.get("command")
    if not isinstance(command, str):
        return PermissionCheckResult("passthrough")
    if _is_dangerous_command(command):
        return PermissionCheckResult("deny", f"危险命令被阻止：{command[:80]}", bypass_immune=True)
    return PermissionCheckResult("passthrough")


@tool(model=Shell, description="执行 shell 命令并返回输出",
      permission=ToolPermission(
          specifier_arg="command",
          tips="{command}",
          check_permissions=check_shell_permissions,
      ))
async def shell(command: str, timeout: int) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"命令超时（{timeout}秒）"

    parts = []
    if stdout:
        parts.append(stdout.decode(errors="replace"))
    if stderr:
        parts.append(f"[stderr]\n{stderr.decode(errors='replace')}")
    if not parts:
        return f"（无输出，退出码：{proc.returncode}）"
    if proc.returncode != 0:
        parts.append(f"[退出码: {proc.returncode}]")
    return "\n".join(parts)
