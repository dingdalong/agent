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

# 无条件只读命令 — 无论参数如何都不会修改文件系统或外部状态
READONLY_COMMANDS: frozenset[str] = frozenset({
    # 文件系统查看
    "ls", "dir", "vdir", "tree", "stat", "file", "readlink", "du", "df",
    # 文本查看
    "cat", "head", "tail", "less", "more", "bat",
    # 文本处理（纯过滤器，不修改文件）
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "wc", "sort", "uniq", "cut", "paste", "tr",
    "column", "fold", "fmt", "rev", "nl", "expand", "unexpand",
    "comm", "diff", "colordiff", "cmp",
    "strings", "od", "xxd", "hexdump",
    "jq", "yq",
    "awk", "gawk", "mawk",
    # 路径/命令查找
    "which", "whereis", "type",
    # Shell 内建 / 系统信息
    "echo", "printf", "true", "false", "test", "[",
    "pwd", "realpath", "dirname", "basename",
    "date", "cal", "uptime", "uname", "hostname",
    "whoami", "id", "groups", "who", "w",
    "arch", "nproc", "getconf",
    # 环境查看
    "printenv",
    # 进程查看
    "ps", "pgrep",
    # 网络查看（只读探测）
    "ping", "dig", "nslookup", "host",
    # 帮助信息
    "man", "info", "help", "apropos", "whatis",
    # 校验 / 编码
    "md5sum", "sha1sum", "sha256sum", "shasum", "b2sum", "cksum",
    "base64",
})

# git 只读子命令 — 部分需要额外参数检查（branch/tag/stash/config）
GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "stash", "describe", "rev-parse", "ls-files", "ls-tree",
    "cat-file", "shortlog", "blame", "reflog", "name-rev",
    "rev-list", "for-each-ref", "worktree", "config",
    "version", "help",
})


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


def _has_output_redirect(segment: list[str]) -> bool:
    """检查命令段是否包含指向非 /dev/null 目标的输出重定向。

    安全的重定向模式：> /dev/null、>> /dev/null、2>/dev/null、2>&1、>&2 等。
    不安全：> file.txt、>> file.txt 等写入普通文件的重定向。

    Args:
        segment: 单个命令段的 token 列表。

    Returns:
        True 表示段中有文件写入重定向，应视为非只读。
    """
    for i, token in enumerate(segment):
        # >&N 或 N>&M 形式（文件描述符复制），安全
        if token == ">&" or token == ">&1" or token == ">&2":
            continue
        if token in {">", ">>"}:
            target = segment[i + 1] if i + 1 < len(segment) else ""
            if target != "/dev/null":
                return True
            continue
        if token.startswith(">") and token not in {">", ">>"}:
            # >& 开头为文件描述符复制（如 >&2），安全
            if token.startswith(">&"):
                continue
            target = token.lstrip(">")
            if target and target != "/dev/null":
                return True
            continue
        # 数字重定向：2>/dev/null, 1>file, 2>&1 等
        if len(token) > 1 and token[0].isdigit() and ">" in token:
            after_gt = token.split(">", 1)[1]
            if after_gt.startswith("&"):
                continue
            if after_gt and after_gt != "/dev/null":
                return True
    return False


def _is_readonly_find(segment: list[str]) -> bool:
    """检查 find 命令段是否只读。

    -delete 使 find 非只读；-exec/-execdir 需要递归检查被执行的命令。

    Args:
        segment: 以 find 开头的 token 列表。

    Returns:
        True 表示该 find 命令是只读的。
    """
    if "-delete" in segment:
        return False
    i = 1
    while i < len(segment):
        if segment[i] in {"-exec", "-execdir"}:
            exec_tokens: list[str] = []
            for j in range(i + 1, len(segment)):
                if segment[j] in {"\\;", ";", "+"}:
                    break
                if segment[j] != "{}":
                    exec_tokens.append(segment[j])
            if exec_tokens and not _is_readonly_segment(exec_tokens):
                return False
        i += 1
    return True


def _is_readonly_git(segment: list[str]) -> bool:
    """检查 git 命令段是否只读。

    提取子命令后与 GIT_READONLY_SUBCOMMANDS 比对，
    对 branch/tag/stash/config 做额外的破坏性参数检测。

    Args:
        segment: 以 git 开头的 token 列表。

    Returns:
        True 表示该 git 命令是只读的。
    """
    # 跳过 git 全局选项，提取子命令
    GIT_GLOBAL_FLAGS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "-c"}
    subcmd = None
    subcmd_index = 0
    i = 1
    while i < len(segment):
        token = segment[i]
        if token in GIT_GLOBAL_FLAGS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        subcmd = token
        subcmd_index = i
        break

    if subcmd is None or subcmd not in GIT_READONLY_SUBCOMMANDS:
        return False

    rest = segment[subcmd_index + 1:]

    if subcmd == "branch":
        BRANCH_WRITE_FLAGS = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy"}
        if any(t in BRANCH_WRITE_FLAGS for t in rest):
            return False

    elif subcmd == "tag":
        TAG_WRITE_FLAGS = {"-d", "--delete", "-a", "-s", "-f", "--force"}
        if any(t in TAG_WRITE_FLAGS for t in rest):
            return False

    elif subcmd == "stash":
        # git stash list / git stash show 只读，其余非只读
        stash_sub = rest[0] if rest else None
        if stash_sub not in {"list", "show", None}:
            return False

    elif subcmd == "config":
        CONFIG_READ_FLAGS = {"--get", "--get-all", "--list", "-l", "--get-regexp"}
        if not any(t in CONFIG_READ_FLAGS for t in rest):
            return False

    return True


def _is_readonly_sed(segment: list[str]) -> bool:
    """检查 sed 命令段是否只读（无 -i 原地编辑）。

    Args:
        segment: 以 sed/gsed 开头的 token 列表。

    Returns:
        True 表示 sed 仅输出到 stdout，不修改文件。
    """
    for token in segment[1:]:
        if token == "--":
            break
        if token == "--in-place" or token == "-i":
            return False
        # -i.bak 形式
        if token.startswith("-i") and not token.startswith("--"):
            return False
        # 组合短选项如 -ni
        if token.startswith("-") and not token.startswith("--") and "i" in token[1:]:
            return False
    return True


def _is_readonly_curl(segment: list[str]) -> bool:
    """检查 curl 命令段是否只读（无输出到文件）。

    Args:
        segment: 以 curl 开头的 token 列表。

    Returns:
        True 表示 curl 仅输出到 stdout。
    """
    for token in segment[1:]:
        if token in {"-o", "-O", "--output", "--create-dirs"}:
            return False
        if token.startswith("-") and not token.startswith("--"):
            if "o" in token[1:] or "O" in token[1:]:
                return False
    return True


def _is_readonly_xargs(segment: list[str]) -> bool:
    """检查 xargs 命令段是否只读。

    跳过 xargs 自身参数后提取被执行的命令，递归检查其是否只读。
    无命令时 xargs 默认执行 echo（只读）。

    Args:
        segment: 以 xargs 开头的 token 列表。

    Returns:
        True 表示 xargs 执行的命令是只读的。
    """
    XARGS_FLAGS_WITH_VALUE = {"-I", "-L", "-n", "-P", "-s", "-E", "--max-args",
                              "--max-procs", "--replace", "--max-lines", "--max-chars",
                              "--eof", "-d", "--delimiter"}
    XARGS_FLAGS_NO_VALUE = {"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose",
                            "-p", "--interactive", "--show-limits", "-x", "--exit"}
    i = 1
    while i < len(segment):
        token = segment[i]
        if token in XARGS_FLAGS_WITH_VALUE:
            i += 2
            continue
        if token in XARGS_FLAGS_NO_VALUE or (token.startswith("-") and not token.startswith("--")):
            i += 1
            continue
        # 到达被执行的命令
        return _is_readonly_segment(segment[i:])
    # 无命令，xargs 默认执行 echo
    return True


def _is_readonly_segment(segment: list[str]) -> bool:
    """检查单个命令段是否只读。

    依次检查：去壳 → 重定向 → 无条件只读集合 → 特殊命令分发。
    未识别的命令保守返回 False。

    Args:
        segment: 经过分隔符分割后的单个命令段 token 列表。

    Returns:
        True 表示该段不会产生写操作。
    """
    segment = _strip_shell_wrappers(segment)
    if not segment:
        return True
    if _has_output_redirect(segment):
        return False
    command = _command_name(segment[0])
    if command in READONLY_COMMANDS:
        return True
    if command == "find":
        return _is_readonly_find(segment)
    if command == "git":
        return _is_readonly_git(segment)
    if command in {"sed", "gsed"}:
        return _is_readonly_sed(segment)
    if command == "curl":
        return _is_readonly_curl(segment)
    if command == "xargs":
        return _is_readonly_xargs(segment)
    # shell -c "..." 递归检查内部命令
    if command in SHELL_SCRIPT_INTERPRETERS:
        inner = _shell_c_command(segment)
        if inner is not None:
            return _is_readonly_command(inner)
    return False


def _is_readonly_command(command: str) -> bool:
    """检查整个 shell 命令字符串是否只读。

    复合命令（; && || | 等）中任一段非只读则整个命令非只读。

    Args:
        command: 待检查的完整 shell 命令字符串。

    Returns:
        True 表示命令不会产生任何写操作。
    """
    if "`" in command:
        return False
    try:
        segments: list[list[str]] = []
        for part in _split_unquoted_newlines(command):
            segments.extend(_shell_segments(_shell_tokens(part)))
    except ValueError:
        return False
    for segment in segments:
        # 管道连接符本身不影响只读判断
        if segment == ["|"]:
            continue
        if not _is_readonly_segment(segment):
            return False
    return True


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
    """shell 安全检查：检测危险命令阻止执行，识别只读命令自动放行。

    评估顺序：危险 → deny，只读 → allow，其余 → passthrough（由后续流程决定）。

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
    if _is_readonly_command(command):
        return PermissionCheckResult("allow", f"只读命令自动放行：{command[:80]}")
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
