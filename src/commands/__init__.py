"""斜杠命令框架 — 装饰器注册 + 统一管理器。

镜像 tools 系统的「注册/发现 + 管理器」模式：
- 命令文件：单个 <name>.py，用 @command 装饰器声明元数据 + 被装饰的 async 执行体。
- CommandMgr 三层扫描（内置 → 全局 → 项目）同名覆盖，统一 dispatch。
"""

from src.commands.context import (
    CommandAppServices,
    CommandContext,
    CommandResult,
)
from src.commands.decorator import command
from src.commands.mgr import (
    CommandEntry,
    CommandMgr,
    CommandOutcome,
    CommandSignal,
)

__all__ = [
    "CommandAppServices",
    "CommandContext",
    "CommandResult",
    "command",
    "CommandEntry",
    "CommandMgr",
    "CommandOutcome",
    "CommandSignal",
]
