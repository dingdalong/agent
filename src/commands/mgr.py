"""斜杠命令管理器 — 三层扫描（内置 → 全局 → 项目），同名覆盖；统一分发。

发现机制镜像 tools 系统：扫描各层目录下的 <name>.py，import 触发 @command 装饰器
把 CommandEntry 登记到模块级 _COMMAND_REGISTRY，再按层收集。
命令的声明（装饰器元数据）与执行体（被装饰的 async 函数）内聚于单个 .py 文件。

分层覆盖：内置 builtin → 全局 user → 项目 project，后者同名覆盖前者。
项目层整层受 project_trusted 门控（对齐 hooks/plugins/MCP）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from src.mgr.paths import builtin_root, project_data_dir

if TYPE_CHECKING:
    from src.agent.states import RunContext
    from src.commands.context import CommandContext

logger = logging.getLogger(__name__)


# 命令 handler 统一签名：async def <name>(ctx, args) -> CommandResult | None
CommandRunner = Callable[["CommandContext", list[str]], Awaitable[object]]


class CommandSignal(Enum):
    """dispatch 给调用方的指令。"""

    HANDLED = "handled"        # 已处理 → Agent 回 REQUEST_INPUT
    DEFER_TO_APP = "defer"     # app 层命令 → run_ctx.command 已挂，Agent 应 DONE 上抛


@dataclass
class CommandOutcome:
    """dispatch 的返回结果。

    Attributes:
        signal: 给调用方的指令。
        new_agent: 仅 app 层 dispatch（AgentApp.run）有意义——/clear 替换前台 Agent。
    """

    signal: CommandSignal
    new_agent: "object | None" = None


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


@dataclass
class CommandMgr:
    """进程级命令注册表；进 AgentDeps，bootstrap 构造，/clear 时 reload。

    Args:
        workdir: 用户工作目录。
        global_dir: 全局配置目录（~/.agent/），None 跳过用户层。
        project_trusted: 项目是否已信任；False 跳过项目层。
    """

    workdir: Path
    global_dir: Path | None = None
    project_trusted: bool = False

    _commands: dict[str, CommandEntry] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        """清缓存并重扫（/clear 时随 reload 链调用，幂等）。"""
        self._commands.clear()
        self._load_all()

    def _load_all(self) -> None:
        """按低→高优先级分层扫描 .py，import 触发装饰器登记，后者同名覆盖前者。"""
        from src.commands.decorator import _COMMAND_REGISTRY

        scan_dirs: list[tuple[Path, str]] = [
            (builtin_root() / "commands" / "builtin", "builtin"),
        ]
        if self.global_dir:
            scan_dirs.append((self.global_dir / "commands", "user"))
        if self.project_trusted:
            scan_dirs.append((project_data_dir(self.workdir) / "commands", "project"))

        for directory, namespace in scan_dirs:
            if not directory.exists():
                continue
            for py_path in sorted(directory.glob("*.py")):
                if py_path.name == "__init__.py":
                    continue
                before = len(_COMMAND_REGISTRY)
                if self._import_module(py_path, namespace) is None:
                    # import 失败已告警；清掉可能部分登记的 entry，避免脏数据
                    del _COMMAND_REGISTRY[before:]
                    continue
                for entry in _COMMAND_REGISTRY[before:]:
                    entry.namespace = namespace
                    self._commands[entry.name] = entry
                    for alias in entry.aliases:
                        self._commands[alias] = entry
                del _COMMAND_REGISTRY[before:]

    def _import_module(self, py_path: Path, namespace: str):
        """按文件路径 import 一个命令模块（触发其 @command 装饰器登记）。

        使用唯一合成模块名，不注册 sys.modules（避免 reload 残留与跨层重名碰撞）。
        import 成功返回模块，失败返回 None（已告警）。
        """
        digest = hashlib.sha1(str(py_path).encode()).hexdigest()[:12]
        module_name = f"_agent_slashcmd_{namespace}_{py_path.stem}_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, py_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.warning("命令实现加载失败：%s", py_path, exc_info=True)
            return None
        return module

    async def dispatch(
        self,
        name: str,
        args: list[str],
        ctx: "CommandContext",
        run_ctx: "RunContext | None" = None,
    ) -> CommandOutcome:
        """统一分发入口。

        agent 层调用（Agent._on_request_input）传 run_ctx（供 defer 挂命令）；
        app 层调用（AgentApp.run，defer 后）不传。
        """
        entry = self._commands.get(name)
        if entry is None:
            await ctx.deps.event_bus.request_output(f"未知命令: /{name}\n")
            return CommandOutcome(CommandSignal.HANDLED)

        if not self._feature_enabled(entry, ctx):
            await ctx.deps.event_bus.request_output(f"命令 /{name} 在当前角色不可用。\n")
            return CommandOutcome(CommandSignal.HANDLED)

        # app 层命令不在 agent 内执行：挂上抛通道，由主循环二次 dispatch。
        if entry.layer == "app" and ctx.app is None:
            if run_ctx is not None:
                run_ctx.command = (entry.name, args)
            return CommandOutcome(CommandSignal.DEFER_TO_APP)

        result = await entry.run(ctx, args)
        return CommandOutcome(
            CommandSignal.HANDLED,
            new_agent=getattr(result, "new_agent", None),
        )

    def _feature_enabled(self, entry: CommandEntry, ctx: "CommandContext") -> bool:
        """feature 门控：未声明 feature 恒可用；声明则要求当前 agent 启用了该 feature。"""
        if entry.feature is None:
            return True
        features = ctx.agent.features if ctx.agent is not None else None
        return features is None or entry.feature in features

    def completion_items(self, features: set[str] | None = None) -> list[tuple[str, str]]:
        """TUI 补全数据源：可见、feature 启用、去别名重复，按名排序。"""
        result: list[tuple[str, str]] = []
        for key in sorted(self._commands):
            entry = self._commands[key]
            if key != entry.name or entry.hidden:
                continue
            if features is not None and entry.feature is not None and entry.feature not in features:
                continue
            result.append((entry.name, entry.description))
        return result

    def list_commands(self, features: set[str] | None = None) -> list[CommandEntry]:
        """/help 用：可见命令 entry 列表（去别名、过滤 hidden/feature，按名排序）。"""
        result: list[CommandEntry] = []
        for key in sorted(self._commands):
            entry = self._commands[key]
            if key != entry.name or entry.hidden:
                continue
            if features is not None and entry.feature is not None and entry.feature not in features:
                continue
            result.append(entry)
        return result
