"""斜杠命令注册装饰器 — 命令的声明与执行体内聚于单个 .py 文件。

镜像 src/tools/decorator.py 的模式：装饰器在 import 时把 CommandEntry 追加到模块级
_COMMAND_REGISTRY；CommandMgr 扫描命令目录、import 每个 .py 触发登记，再按层收集。
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from src.commands.mgr import CommandEntry

if TYPE_CHECKING:
    from src.commands.context import CommandContext

logger = logging.getLogger(__name__)

# 命令 handler 统一签名：async def <name>(ctx, args) -> CommandResult | None
CommandRunner = Callable[["CommandContext", list[str]], "object"]

# 模块级注册表：import 命令模块时由装饰器追加；CommandMgr 收集后清空。
_COMMAND_REGISTRY: list[CommandEntry] = []


def command(
    description: str,
    *,
    name: str | None = None,
    usage: str | None = None,
    aliases: tuple[str, ...] = (),
    layer: str = "agent",
    feature: str | None = None,
    hidden: bool = False,
) -> Callable:
    """斜杠命令注册装饰器。

    Args:
        description: 命令描述；补全与 /help 展示。
        name: 命令名（小写，不含 / 前缀），默认使用函数名。
        usage: 用法行，缺省 "/<name>"。
        aliases: 别名列表，注册到同一 entry。
        layer: agent(默认) | app；app = 需主循环上下文（reset 会话等）。
        feature: 所属可插拔 feature 名（对齐 ALL_FEATURES）；未启用时不可用且不出现。
        hidden: True 时可执行但不进补全与 /help 列表。

    Returns:
        装饰后的原函数。
    """
    def decorator(func: CommandRunner) -> CommandRunner:
        cmd_name = (name or func.__name__).strip().lower()
        try:
            path = Path(inspect.getsourcefile(func) or "")
        except (TypeError, OSError):  # pragma: no cover - 防御
            path = Path("")
        _COMMAND_REGISTRY.append(CommandEntry(
            name=cmd_name,
            description=description,
            run=func,
            usage=usage or f"/{cmd_name}",
            aliases=aliases,
            layer=layer,
            feature=feature,
            hidden=hidden,
            path=path,
            namespace="",  # 由 CommandMgr 按所在层填充
        ))
        return func

    return decorator
