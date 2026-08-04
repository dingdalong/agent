"""Agent /models 命令的回归测试。"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext
from src.mgr.llm_mgr import LLMMgr


class ConfigStub:
    """提供点路径读取的最小配置管理器。"""

    def __init__(self, config: dict[str, Any]) -> None:
        """保存测试配置。"""
        self.config = config

    def get_config(self, key: str) -> Any:
        """按点路径返回配置值。"""
        value: Any = self.config
        for part in key.split("."):
            value = value[part]
        return value


class RecordingEventBus:
    """记录输出文本并按顺序提供交互输入。"""

    def __init__(self, inputs: list[str] | None = None) -> None:
        """初始化事件总线。"""
        self.inputs = iter(inputs or [])
        self.outputs: list[str] = []

    async def emit(self, event: object) -> None:
        """忽略事件记录。"""

    async def request_input(self, prompt: str, default: str = "") -> str:
        """返回下一条预设输入。"""
        del prompt, default
        return next(self.inputs)

    async def request_output(self, content: str, **kwargs: object) -> None:
        """记录请求输出文本。"""
        del kwargs
        self.outputs.append(content)


def _config(llm: dict[str, Any]) -> dict[str, Any]:
    """构造包含给定 llm 别名段的最小配置。"""
    return {
        "llm": {
            "concurrency": 5,
            "timeout_seconds": 120,
            "retry": {
                "max_attempts": 3,
                "base_delay_seconds": 2,
                "max_delay_seconds": 60,
            },
            **llm,
        },
        "llm_provider": {
            "stub": {
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
            },
        },
        "tool": {"page_token_rate": 0.03},
    }


def _llm_mgr(
    model_to_provider: dict[str, str],
    llm: dict[str, Any],
) -> LLMMgr:
    """构造播种了模型注册表与别名的 LLM 管理器（不连接网络）。"""
    manager = LLMMgr(config_mgr=ConfigStub(_config(llm)), event_bus=None)
    manager._model_to_provider.update(model_to_provider)
    return manager


def _agent(llm_mgr: LLMMgr, event_bus: RecordingEventBus) -> Agent:
    """构造仅携带 /models 命令所需依赖的最小 Agent。"""
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.deps = SimpleNamespace(event_bus=event_bus, llm_mgr=llm_mgr)
    return agent


def _run_models(agent: Agent) -> str:
    """驱动 /models 处理器并返回记录到的输出文本。"""
    asyncio.run(agent._handle_models_command())
    return agent.deps.event_bus.outputs[0]


def test_models_command_groups_and_marks_aliases() -> None:
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
    bus = RecordingEventBus()

    output = _run_models(_agent(llm_mgr, bus))

    assert "anthropic:" in output
    assert "deepseek:" in output
    assert "  - deepseek-v4-flash [default]" in output
    assert "  - deepseek-v4-pro [fast]" in output
    assert "  - claude-opus-4-8 [best]" in output
    assert "  - claude-sonnet-5\n" in output


def test_models_command_merges_missing_best_fast_into_default() -> None:
    """仅设 default 时 best/fast 回退并合并标注到同一模型。"""
    llm_mgr = _llm_mgr(
        {"deepseek-v4-flash": "deepseek"},
        {"default": "deepseek-v4-flash"},
    )
    bus = RecordingEventBus()

    output = _run_models(_agent(llm_mgr, bus))

    assert "  - deepseek-v4-flash [default, best, fast]" in output


def test_models_command_empty() -> None:
    """空注册表应输出无可用模型提示。"""
    llm_mgr = _llm_mgr({}, {"default": "model-a"})
    bus = RecordingEventBus()

    output = _run_models(_agent(llm_mgr, bus))

    assert output == "当前没有可用模型。\n"


def test_models_command_dispatch_returns_request_input() -> None:
    """/models 输入应走命令分发，打印后返回 REQUEST_INPUT 且不设置 ctx.command。"""
    llm_mgr = _llm_mgr(
        {"deepseek-v4-flash": "deepseek"},
        {"default": "deepseek-v4-flash"},
    )
    bus = RecordingEventBus(inputs=["/models"])
    agent = _agent(llm_mgr, bus)
    ctx = RunContext(messages=[])

    state = asyncio.run(agent._on_request_input(ctx))

    assert state is AgentState.REQUEST_INPUT
    assert ctx.command is None
    assert bus.outputs and "deepseek-v4-flash" in bus.outputs[0]
