"""CommandMgr 的发现、分层覆盖、信任门控、feature 门控与分发测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.commands.context import CommandContext
from src.commands.mgr import CommandMgr, CommandSignal


class RecordingEventBus:
    def __init__(self) -> None:
        self.outputs: list[str] = []

    async def request_output(self, content: str, **kwargs: object) -> None:
        self.outputs.append(content)


def _write_command(directory: Path, name: str, *, decorator_args: str = '"测试命令"',
                   body: str | None = None) -> None:
    """在指定目录写一个用 @command 装饰器注册的 <name>.py 命令。"""
    directory.mkdir(parents=True, exist_ok=True)
    py = directory / f"{name}.py"
    py.write_text(
        "from src.commands import command\n"
        "from src.commands.context import CommandContext\n"
        f"@command({decorator_args})\n"
        f"async def {name}(ctx: CommandContext, args: list):\n"
        + (body if body is not None else "    await ctx.deps.event_bus.request_output('ran\\n')\n"),
        encoding="utf-8",
    )


def _ctx(features: set[str] | None = None) -> CommandContext:
    agent = SimpleNamespace(features=features) if features is not None else None
    return CommandContext(deps=SimpleNamespace(event_bus=RecordingEventBus()), agent=agent)


# ── 发现与分层覆盖 ────────────────────────────────────────────────────

def test_builtin_commands_discovered(tmp_path: Path) -> None:
    """内置目录的六个命令应被发现。"""
    mgr = CommandMgr(workdir=tmp_path, global_dir=None, project_trusted=False)
    names = {e.name for e in mgr._commands.values()}
    assert {"plan", "clear", "resume", "agents", "models", "help"} <= names


def test_global_layer_overrides_builtin(tmp_path: Path) -> None:
    """全局层同名命令覆盖内置。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "models", decorator_args='"覆盖版 models"')
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert mgr._commands["models"].description == "覆盖版 models"
    assert mgr._commands["models"].namespace == "user"


def test_project_layer_requires_trust(tmp_path: Path) -> None:
    """项目层整层受 project_trusted 门控。"""
    workdir = tmp_path / "work"
    _write_command(workdir / ".agent" / "commands", "proj")

    untrusted = CommandMgr(workdir=workdir, global_dir=None, project_trusted=False)
    assert "proj" not in untrusted._commands

    trusted = CommandMgr(workdir=workdir, global_dir=None, project_trusted=True)
    assert "proj" in trusted._commands
    assert trusted._commands["proj"].namespace == "project"


def test_import_failure_skips_module(tmp_path: Path) -> None:
    """import 失败（语法错）的模块整体跳过。"""
    global_dir = tmp_path / "global"
    (global_dir / "commands").mkdir(parents=True)
    (global_dir / "commands" / "broken.py").write_text("def (:", encoding="utf-8")
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert "broken" not in mgr._commands


def test_module_without_command_registers_nothing(tmp_path: Path) -> None:
    """无 @command 装饰器的模块不产生任何 entry。"""
    global_dir = tmp_path / "global"
    (global_dir / "commands").mkdir(parents=True)
    (global_dir / "commands" / "plain.py").write_text("X = 1\n", encoding="utf-8")
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert "plain" not in mgr._commands


def test_aliases_registered(tmp_path: Path) -> None:
    """别名注册到同一 entry，但不进补全。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "mycmd",
                   decorator_args='"测试命令", aliases=("mc", "c")')
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert mgr._commands["mc"] is mgr._commands["mycmd"]
    assert mgr._commands["c"] is mgr._commands["mycmd"]
    names = [name for name, _ in mgr.completion_items()]
    assert "mycmd" in names and "mc" not in names and "c" not in names


def test_hidden_excluded_from_completion(tmp_path: Path) -> None:
    """hidden 命令可执行但不进补全与 list_commands。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "secret",
                   decorator_args='"测试命令", hidden=True')
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert "secret" in mgr._commands
    assert "secret" not in [n for n, _ in mgr.completion_items()]
    assert "secret" not in [e.name for e in mgr.list_commands()]


def test_reload_rescans(tmp_path: Path) -> None:
    """reload 后新命令出现。"""
    global_dir = tmp_path / "global"
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    assert "late" not in mgr._commands
    _write_command(global_dir / "commands", "late")
    mgr.reload()
    assert "late" in mgr._commands


# ── 分发 ─────────────────────────────────────────────────────────────

def test_dispatch_unknown_command(tmp_path: Path) -> None:
    """未知命令输出提示并返回 HANDLED。"""
    mgr = CommandMgr(workdir=tmp_path, global_dir=None, project_trusted=False)
    ctx = _ctx()
    outcome = asyncio.run(mgr.dispatch("nosuch", [], ctx))
    assert outcome.signal is CommandSignal.HANDLED
    assert ctx.deps.event_bus.outputs == ["未知命令: /nosuch\n"]


def test_dispatch_runs_handler(tmp_path: Path) -> None:
    """已注册命令执行其 handler。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "hello")
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)
    ctx = _ctx()
    outcome = asyncio.run(mgr.dispatch("hello", [], ctx))
    assert outcome.signal is CommandSignal.HANDLED
    assert ctx.deps.event_bus.outputs == ["ran\n"]


def test_dispatch_feature_gated(tmp_path: Path) -> None:
    """声明 feature 的命令在未启用时不可用。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "plancmd",
                   decorator_args='"测试命令", feature="plan"')
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)

    ctx_off = _ctx(features={"file"})
    asyncio.run(mgr.dispatch("plancmd", [], ctx_off))
    assert "不可用" in ctx_off.deps.event_bus.outputs[0]

    ctx_on = _ctx(features={"plan"})
    asyncio.run(mgr.dispatch("plancmd", [], ctx_on))
    assert ctx_on.deps.event_bus.outputs == ["ran\n"]

    assert "plancmd" not in [n for n, _ in mgr.completion_items(features={"file"})]
    assert "plancmd" in [n for n, _ in mgr.completion_items(features={"plan"})]


def test_dispatch_app_layer_defers_in_agent(tmp_path: Path) -> None:
    """app 层命令在 agent 内（app=None）defer：挂 run_ctx.command 并返回 DEFER_TO_APP。"""
    global_dir = tmp_path / "global"
    _write_command(global_dir / "commands", "wipe",
                   decorator_args='"测试命令", layer="app"')
    mgr = CommandMgr(workdir=tmp_path, global_dir=global_dir, project_trusted=False)

    run_ctx = SimpleNamespace(command=None)
    ctx = _ctx()  # app=None
    outcome = asyncio.run(mgr.dispatch("wipe", ["x"], ctx, run_ctx=run_ctx))
    assert outcome.signal is CommandSignal.DEFER_TO_APP
    assert run_ctx.command == ("wipe", ["x"])
    assert ctx.deps.event_bus.outputs == []
