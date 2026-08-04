"""内置斜杠命令 handler 的单元测试。

handler 是模块级 `async def run(ctx, args)`，CommandContext 用轻量 stub 最小构造，
无需实例化整个 Agent/AgentApp。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from src.commands.context import CommandContext, CommandResult
from src.commands.builtin import agents as agents_cmd
from src.commands.builtin import clear as clear_cmd
from src.commands.builtin import help as help_cmd
from src.commands.builtin import models as models_cmd
from src.commands.builtin import plan as plan_cmd
from src.mgr.llm_mgr import LLMMgr


class ConfigStub:
    """提供点路径读取的最小配置管理器。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def get_config(self, key: str) -> Any:
        value: Any = self.config
        for part in key.split("."):
            value = value[part]
        return value


class RecordingEventBus:
    """记录输出文本与事件，并按顺序提供交互输入。"""

    def __init__(self, inputs: list[str] | None = None) -> None:
        self.inputs = iter(inputs or [])
        self.outputs: list[str] = []
        self.events: list[object] = []
        self.choices: list[tuple] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)

    async def request_output(self, content: str, **kwargs: object) -> None:
        self.outputs.append(content)

    async def request_choice(self, prompt: str, options: list, default: int = 0, **kw: object):
        self.choices.append((prompt, options))
        return ""

    async def request_transcript_view(self, uid: str) -> None:
        pass


def _ctx(**deps_kwargs: Any) -> CommandContext:
    """构造携带给定依赖的 CommandContext（deps 为 SimpleNamespace）。"""
    agent = deps_kwargs.pop("agent", None)
    app = deps_kwargs.pop("app", None)
    return CommandContext(deps=SimpleNamespace(**deps_kwargs), agent=agent, app=app)


# ── /models ──────────────────────────────────────────────────────────

def _config(llm: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm": {
            "concurrency": 5,
            "timeout_seconds": 120,
            "retry": {"max_attempts": 3, "base_delay_seconds": 2, "max_delay_seconds": 60},
            **llm,
        },
        "llm_provider": {"stub": {"api_key": "test-key", "base_url": "https://example.test/v1"}},
        "tool": {"page_token_rate": 0.03},
    }


def _llm_mgr(model_to_provider: dict[str, str], llm: dict[str, Any]) -> LLMMgr:
    manager = LLMMgr(config_mgr=ConfigStub(_config(llm)), event_bus=None)
    manager._model_to_provider.update(model_to_provider)
    return manager


def _run_models(llm_mgr: LLMMgr) -> str:
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, llm_mgr=llm_mgr)
    asyncio.run(models_cmd.models(ctx, []))
    return bus.outputs[0]


def test_models_groups_and_marks_aliases() -> None:
    """应按 provider 分组并标注 default/best/fast 指向的模型。"""
    llm_mgr = _llm_mgr(
        {
            "claude-opus-4-8": "anthropic",
            "claude-sonnet-5": "anthropic",
            "deepseek-v4-pro": "deepseek",
            "deepseek-v4-flash": "deepseek",
        },
        {"default": "deepseek-v4-flash", "best": "claude-opus-4-8", "fast": "deepseek-v4-pro"},
    )
    output = _run_models(llm_mgr)
    assert "anthropic:" in output
    assert "deepseek:" in output
    assert "  - deepseek-v4-flash [default]" in output
    assert "  - deepseek-v4-pro [fast]" in output
    assert "  - claude-opus-4-8 [best]" in output
    assert "  - claude-sonnet-5\n" in output


def test_models_merges_missing_best_fast_into_default() -> None:
    """仅设 default 时 best/fast 回退并合并标注到同一模型。"""
    llm_mgr = _llm_mgr({"deepseek-v4-flash": "deepseek"}, {"default": "deepseek-v4-flash"})
    assert "  - deepseek-v4-flash [default, best, fast]" in _run_models(llm_mgr)


def test_models_empty() -> None:
    """空注册表应输出无可用模型提示。"""
    llm_mgr = _llm_mgr({}, {"default": "model-a"})
    assert _run_models(llm_mgr) == "当前没有可用模型。\n"


# ── /plan ────────────────────────────────────────────────────────────

def _plan_agent(can_enter: bool) -> SimpleNamespace:
    agent = SimpleNamespace(agent_type="main", uuid=uuid.uuid4())
    agent.set_plan_active = lambda active: can_enter
    return agent


def test_plan_enters_mode_and_emits_event() -> None:
    """成功进入计划模式：发 PlanStateChanged 事件并输出提示。"""
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, agent=_plan_agent(can_enter=True))
    asyncio.run(plan_cmd.plan(ctx, []))
    assert bus.outputs == ["已进入计划模式。\n"]
    assert len(bus.events) == 1  # PlanStateChanged


def test_plan_already_active() -> None:
    """已在计划模式时仅提示、不发事件。"""
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, agent=_plan_agent(can_enter=False))
    asyncio.run(plan_cmd.plan(ctx, []))
    assert bus.outputs == ["已在计划模式中。\n"]
    assert bus.events == []


# ── /clear ───────────────────────────────────────────────────────────

def test_clear_resets_session_and_returns_new_agent() -> None:
    """应调用 app.reset_session(source=clear) 并把新 agent 放进结果。"""
    new_agent = SimpleNamespace(uuid="new", agent_type="main")
    calls: list[str] = []

    class FakeApp:
        async def reset_session(self, *, source: str = "clear"):
            calls.append(source)
            return new_agent

    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, app=FakeApp())
    result = asyncio.run(clear_cmd.clear(ctx, []))
    assert isinstance(result, CommandResult)
    assert result.new_agent is new_agent
    assert calls == ["clear"]
    assert bus.outputs == ["上下文已清理，所有组件已重载。\n"]


# ── /agents ──────────────────────────────────────────────────────────

def test_agents_non_tty_prints_summary() -> None:
    """非 TTY 环境退回打印纯文本摘要。"""
    from src.events.types import SubagentLifecycle
    from src.interfaces.agent_view_store import AgentViewStore

    store = AgentViewStore()
    store.record(SubagentLifecycle(
        timestamp=1.0, source="test", agent_uuid="w-0", agent_type="worker", phase="start",
    ))
    bus = RecordingEventBus()
    ctx = _ctx(
        event_bus=bus,
        ui=SimpleNamespace(is_tty=False),
        app=SimpleNamespace(agent_view_store=store),
    )
    asyncio.run(agents_cmd.agents(ctx, []))
    assert bus.outputs and "子 agent" in bus.outputs[0]


# ── /help ────────────────────────────────────────────────────────────

def test_help_lists_visible_commands() -> None:
    """应列出注册表提供的可见命令 usage 与 description。"""
    entry = SimpleNamespace(usage="/foo", description="做 foo", name="foo", hidden=False, feature=None)
    mgr = SimpleNamespace(list_commands=lambda features=None: [entry])
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, command_mgr=mgr, agent=SimpleNamespace(features=None))
    asyncio.run(help_cmd.help(ctx, []))
    assert "可用命令" in bus.outputs[0]
    assert "/foo" in bus.outputs[0]
    assert "做 foo" in bus.outputs[0]
