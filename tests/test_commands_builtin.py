"""内置斜杠命令 handler 的单元测试。

handler 是模块级 `async def run(ctx, args)`，CommandContext 用轻量 stub 最小构造，
无需实例化整个 Agent/AgentApp。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands.context import CommandContext, CommandResult
from src.commands.builtin import agents as agents_cmd
from src.commands.builtin import clear as clear_cmd
from src.commands.builtin import help as help_cmd
from src.commands.builtin import models as models_cmd
from src.commands.builtin import plan as plan_cmd
from src.mgr.llm_mgr import LLMMgr
from src.llm.models import split_model_reference


class ConfigStub:
    """提供点路径读取的最小配置管理器。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def get_config(self, key: str) -> Any:
        value: Any = self.config
        for part in key.split("."):
            value = value[part]
        return value

    def get_config_parts(self, parts: tuple[str, ...]) -> Any:
        value: Any = self.config
        for part in parts:
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

def _config(model_slots: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm": {
            "concurrency": 5,
            "timeout_seconds": 120,
            "retry": {"max_attempts": 3, "base_delay_seconds": 2, "max_delay_seconds": 60},
        },
        "llm_provider": {"openai": {"api_key": "test-key", "base_url": "https://example.test/v1"}},
        "role": {"default": "coding", "coding": {"model": model_slots}},
        "tool": {"page_token_rate": 0.03},
    }


def _llm_mgr(model_to_provider: dict[str, str], model_slots: dict[str, Any]) -> LLMMgr:
    config = _config({
        slot: model if "/" in model else f"{model_to_provider.get(model, 'openai')}/{model}"
        for slot, model in model_slots.items()
    })
    for model, provider in model_to_provider.items():
        config["llm_provider"].setdefault(provider, {"base_url": "https://example.test/v1"}).setdefault("models", []).append(model)
    manager = LLMMgr(ConfigStub(config), SimpleNamespace(role_name="coding"), None)
    manager.refresh_models = AsyncMock()
    return manager


def _run_models(llm_mgr: LLMMgr) -> str:
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, llm_mgr=llm_mgr)
    asyncio.run(models_cmd.models(ctx, []))
    return bus.outputs[0]


def test_models_groups_and_marks_aliases() -> None:
    """应按 provider 分组并标注 default/fast 槽位指向的模型。"""
    llm_mgr = _llm_mgr(
        {
            "claude-opus-4-8": "anthropic",
            "claude-sonnet-5": "anthropic",
            "deepseek-v4-pro": "deepseek",
            "deepseek-v4-flash": "deepseek",
        },
        {"default": "deepseek-v4-flash", "fast": "deepseek-v4-pro"},
    )
    output = _run_models(llm_mgr)
    assert "anthropic:" in output
    assert "deepseek:" in output
    assert "  - deepseek-v4-flash [default]" in output
    assert "  - deepseek-v4-pro [fast]" in output
    assert "  - claude-opus-4-8\n" in output
    assert "  - claude-sonnet-5\n" in output


def test_models_merges_slots_pointing_at_same_model() -> None:
    """两个槽位指向同一模型时应合并标注。"""
    llm_mgr = _llm_mgr(
        {"deepseek-v4-flash": "deepseek"},
        {"default": "deepseek-v4-flash", "fast": "deepseek-v4-flash"},
    )
    assert "  - deepseek-v4-flash [default, fast]" in _run_models(llm_mgr)


def test_models_lists_selected_models_without_discovery() -> None:
    """没有候选时仍展示所选模型。"""
    llm_mgr = _llm_mgr({}, {"default": "openai/model-a", "fast": "openai/model-a"})

    assert "model-a [default, fast]" in _run_models(llm_mgr)


class SlotLLM:
    """双槽位解析 + 可控实例化失败的最小 llm_mgr 替身。"""

    def __init__(self, slots: dict[str, str], unavailable: set[str] | None = None) -> None:
        self.slots = slots
        self.unavailable = unavailable or set()
        self.instantiated: list[str] = []
        self.refresh_models = AsyncMock()
        self.provider_errors = {}
        self.providers = {
            "openai/model-a": SimpleNamespace(model="openai/model-a", reasoning_effort="high"),
            "openai/model-b": SimpleNamespace(model="openai/model-b", reasoning_effort="max"),
        }

    def list_models(self) -> list[str]:
        return list(self.providers)

    def resolve_model(self, alias: str) -> str:
        reference = self.slots.get(alias, alias)
        split_model_reference(reference)
        return reference

    def get(self, model: str) -> Any:
        self.instantiated.append(model)
        if model in self.unavailable:
            raise AssertionError("选择不得预实例化模型")
        return self.providers[model]


class SelectionBus(RecordingEventBus):
    """按预设三元组作答模型菜单，并记录请求参数。"""

    def __init__(self, selection: tuple[str, str, str]) -> None:
        super().__init__()
        self.selection = selection

    async def request_model_selection(
        self,
        prompt: str,
        options: list,
        efforts: list,
        default_model_index: int,
        fast_model_index: int,
        effort_index: int,
        **kwargs: object,
    ) -> tuple[str, str, str]:
        self.choices.append(
            (prompt, options, efforts, default_model_index, fast_model_index, effort_index, kwargs)
        )
        return self.selection


class WritableConfig:
    """记录一次批量写入的最小 config_mgr 替身。"""

    def __init__(self, trusted: bool = True) -> None:
        self.project_trusted = trusted
        self.global_config_path = "/tmp/global/config.yaml"
        self.values: dict[tuple[str, ...], Any] | None = None
        self.scope: str | None = None
        self.reloads = 0

    def set_configs_parts(
        self, values: dict[tuple[str, ...], Any], scope: str
    ) -> None:
        self.values = values
        self.scope = scope

    def reload(self) -> None:
        self.reloads += 1


def _run_selection(
    selection: tuple[str, str, str],
    *,
    slots: dict[str, str] | None = None,
    unavailable: set[str] | None = None,
    trusted: bool = True,
    role_name: str | None = "coding",
    agent_model: str = "openai/model-a",
    agent_effort: str | None = "high",
) -> SimpleNamespace:
    """跑一次带交互的 /models，返回记录了各层副作用的命名空间。

    Args:
        selection: 模型菜单返回的 (default 模型, fast 模型, 推理强度) 三元组。
        slots: 当前角色两槽位指向的模型；默认两槽位都是 model-a。
        unavailable: 不得在保存前调用 llm.get() 的模型集合。
        trusted: 项目信任状态。
        role_name: 活动角色名；None 表示当前无法确定活动角色。
        agent_model: 当前 agent 已绑定的模型。
        agent_effort: 当前 agent 的显式推理强度；None 时使用 provider 回退值。

    Returns:
        SimpleNamespace(bus, config, llm, agent, switches)。
    """
    llm = SlotLLM(slots or {"default": "openai/model-a", "fast": "openai/model-a"}, unavailable)
    switches: list[tuple[str, str]] = []
    agent = SimpleNamespace(
        uuid=uuid.uuid4(),
        model=agent_model,
        history=[{"role": "user", "content": "保留我"}],
        llm=llm.providers[agent_model],
        reasoning_effort=agent_effort,
    )

    def switch_model(model: str, effort: str) -> None:
        switches.append((model, effort))
        agent.llm = llm.providers.get(model, SimpleNamespace(model=model, reasoning_effort=effort))
        agent.model = model
        agent.reasoning_effort = effort

    agent.switch_model = switch_model
    bus = SelectionBus(selection)
    config = WritableConfig(trusted)
    ctx = _ctx(
        event_bus=bus,
        llm_mgr=llm,
        config_mgr=config,
        role_mgr=SimpleNamespace(role_name=role_name),
        agent=agent,
    )

    asyncio.run(models_cmd.models(ctx, []))

    return SimpleNamespace(bus=bus, config=config, llm=llm, agent=agent, switches=switches)


def test_models_persists_both_slots_and_switches_agent() -> None:
    """改 default 槽位后应整体写父键 mapping，并原地热切当前 agent。"""
    run = _run_selection(("openai/model-b", "openai/model-a", "xhigh"))

    assert run.switches == [("openai/model-b", "xhigh")]
    assert run.agent.history == [{"role": "user", "content": "保留我"}]
    assert run.config.values == {
        ("role", "coding", "model"): {"default": "openai/model-b", "fast": "openai/model-a"},
        ("role", "coding", "reasoning_effort"): "xhigh",
    }
    assert run.config.scope == "project"
    assert run.config.reloads == 1
    assert run.llm.instantiated == []
    request = run.bus.choices[0]
    assert request[0] == ""
    assert [label for _value, label in request[1]] == ["openai/model-a", "openai/model-b"]
    assert request[2] == ["low", "medium", "high", "xhigh", "max"]
    assert request[3:6] == (0, 0, 2)
    message = run.bus.outputs[-1]
    assert "default=openai/model-b" in message
    assert "fast=openai/model-a" in message
    assert "xhigh" in message


def test_models_fast_only_change_skips_agent_switch() -> None:
    """只改 fast 槽位时写配置但不动主 agent。"""
    run = _run_selection(("openai/model-a", "openai/model-b", "high"))

    assert run.switches == []
    assert run.config.values == {
        ("role", "coding", "model"): {"default": "openai/model-a", "fast": "openai/model-b"},
        ("role", "coding", "reasoning_effort"): "high",
    }
    assert run.config.reloads == 1
    assert "fast=openai/model-b" in run.bus.outputs[-1]


def test_models_persists_dotted_role_as_exact_path_segment() -> None:
    """含点角色名在 /models 写回时必须保持为单个 mapping key。"""
    run = _run_selection(
        ("openai/model-a", "openai/model-b", "high"),
        role_name="review.v2",
    )

    assert run.config.values == {
        ("role", "review.v2", "model"): {
            "default": "openai/model-a",
            "fast": "openai/model-b",
        },
        ("role", "review.v2", "reasoning_effort"): "high",
    }


def test_models_fast_only_change_uses_provider_effort_without_switching_agent() -> None:
    """agent 未显式设 effort 时，只改 fast 不应因 provider 回退值触发主 agent 切换。"""
    run = _run_selection(
        ("openai/model-a", "openai/model-b", "high"),
        agent_effort=None,
    )

    assert run.bus.choices[0][5] == 2
    assert run.switches == []
    assert run.config.values == {
        ("role", "coding", "model"): {"default": "openai/model-a", "fast": "openai/model-b"},
        ("role", "coding", "reasoning_effort"): "high",
    }


def test_models_effort_only_change_switches_agent() -> None:
    """只改推理强度也要热切当前 agent。"""
    run = _run_selection(("openai/model-a", "openai/model-a", "max"))

    assert run.switches == [("openai/model-a", "max")]
    assert run.config.values == {
        ("role", "coding", "model"): {"default": "openai/model-a", "fast": "openai/model-a"},
        ("role", "coding", "reasoning_effort"): "max",
    }


def test_models_does_not_preinstantiate_slots() -> None:
    run = _run_selection(("openai/model-b", "openai/model-a", "high"), unavailable={"openai/model-b"})
    assert run.config.values is not None
    assert run.llm.instantiated == []
    run.llm.refresh_models.assert_awaited_once()


def test_models_accepts_selection_outside_candidates() -> None:
    run = _run_selection(("openai/not-listed", "openai/model-a", "high"))
    assert run.config.values[("role", "coding", "model")]["default"] == "openai/not-listed"
    assert run.switches == [("openai/not-listed", "high")]


def test_models_different_provider_same_id_switches_agent() -> None:
    run = _run_selection(("ollama/model-a", "openai/model-a", "high"))
    assert run.switches == [("ollama/model-a", "high")]


def test_models_failed_save_does_not_switch_agent(monkeypatch) -> None:
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(WritableConfig, "set_configs_parts", fail)
    run = _run_selection(("openai/model-b", "openai/model-a", "high"))
    assert run.config.values is None
    assert run.switches == []


def test_models_untrusted_project_refuses_before_any_change() -> None:
    """项目未信任时拒绝执行：不弹菜单、不写配置、不切换。"""
    run = _run_selection(("openai/model-b", "openai/model-b", "max"), trusted=False)

    assert run.bus.choices == []
    assert run.switches == []
    assert run.config.values is None
    message = run.bus.outputs[-1]
    assert "未被信任" in message
    assert 'role["coding"].model' in message
    assert "/tmp/global/config.yaml" in message


def test_models_missing_active_role_refuses_before_menu_or_change() -> None:
    """活动角色名缺失时提前拒绝，不弹菜单、不写配置也不切换。"""
    run = _run_selection(("openai/model-b", "openai/model-b", "max"), role_name=None)

    assert run.bus.choices == []
    assert run.switches == []
    assert run.config.values is None
    assert run.config.reloads == 0
    message = run.bus.outputs[-1]
    assert "活动角色" in message
    assert "未写配置" in message
    assert "未切换模型" in message


def test_models_cancel_keeps_agent_and_config_unchanged() -> None:
    """Esc 取消时不应切换 Agent 或写配置。"""
    run = _run_selection(("", "", ""))

    assert run.switches == []
    assert run.config.values is None
    assert run.bus.outputs == []


def test_models_bus_without_selection_support_degrades_to_plain_listing() -> None:
    """event_bus 没有 request_model_selection 时降级为带槽位标注的纯文本列表。"""
    llm_mgr = _llm_mgr(
        {"openai/model-a": "openai", "openai/model-b": "openai"},
        {"default": "openai/model-a", "fast": "openai/model-b"},
    )
    bus = RecordingEventBus()
    ctx = _ctx(event_bus=bus, llm_mgr=llm_mgr, agent=SimpleNamespace())

    asyncio.run(models_cmd.models(ctx, []))

    assert "  - model-a [default]" in bus.outputs[0]
    assert "  - model-b [fast]" in bus.outputs[0]


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
    assert bus.outputs == []


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
