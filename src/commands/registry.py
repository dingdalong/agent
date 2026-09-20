"""斜杠命令的登记契约与模块级注册表 — 装饰器与管理器共用的叶子模块。

发现机制镜像 tools 系统：`src/commands/decorator.py` 在 import 命令模块时把
`CommandEntry` 追加到 `_COMMAND_REGISTRY`；`src/commands/mgr.py` 按层收集后清空。
契约放在本模块，装饰器与管理器都只依赖它，彼此无须互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from src.commands.context import CommandContext

# 命令 handler 统一签名：async def <name>(ctx, args) -> CommandResult | None
CommandRunner = Callable[["CommandContext", list[str]], Awaitable[object]]


@dataclass
class CommandEntry:
    """一条已注册的斜杠命令。

    Attributes:
        name: 命令名（小写，不含 / 前缀）。
        description: 简述；补全与 /help 展示。
        path: 来源 .py 文件路径（调试用）。
        namespace: 来源层（builtin | user | project）；装饰器置空，由 CommandMgr 填充。
        run: 执行体（被 @command 装饰的 async 函数）。
        usage: 用法行，缺省 "/<name>"。
        aliases: 别名列表，注册到同一 entry。
        layer: agent(默认) | app；app = 需主循环上下文（reset 会话等）。
        feature: 所属可插拔 feature（对齐 ALL_FEATURES）；未启用时不可用且不出现。
        hidden: True 时可执行但不进补全与 /help 列表。
    """

    name: str
    description: str
    run: CommandRunner
    path: Path | None = None
    namespace: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    layer: str = "agent"
    feature: str | None = None
    hidden: bool = False


# 模块级注册表：import 命令模块时由装饰器追加；CommandMgr 收集后清空。
_COMMAND_REGISTRY: list[CommandEntry] = []
