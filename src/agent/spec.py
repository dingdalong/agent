"""Agent 身份与配置 — 装配前的不可变输入。

`AgentSpec` 只做纯数据映射：`AgentManifest` + 调用方 overrides → spec。
它不解析模型、不构造 Manager、不读写文件，因此装配器、委派路径与测试
可以共享同一份事实，而不会触发任何副作用。

字段命名区分「构造请求值」与「运行时权威值」：本类持有
``requested_model`` / ``initial_reasoning_effort`` / ``initial_mode``，
切换模型或模式后的当前值只存在于 `Agent` 自身，避免两处权威。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.common.features import resolve_features
from src.mode import RunMode

if TYPE_CHECKING:
    from src.mgr.role_mgr import AgentManifest


def resolve_memory_scope(manifest_memory: str | None, is_subagent: bool) -> str | None:
    """解析 agent 的记忆范围。

    Args:
        manifest_memory: manifest 中声明的记忆范围，未声明为 None。
        is_subagent: 是否为子 agent。

    Returns:
        记忆范围字符串；manifest 显式声明则用其值，未声明时按类型取默认——
        主 agent → "project"（加载项目记忆），子 agent → None（不加载）。
    """
    if manifest_memory is not None:
        return manifest_memory
    return None if is_subagent else "project"


@dataclass(frozen=True)
class AgentSpec:
    """一次 agent 装配的不可变输入。

    Attributes:
        agent_type: agent 类型标识，与 manifest 的 ``agent_type`` 一致。
        description: 一句话描述。
        is_subagent: 是否以子 agent 身份装配。
        role_prompt: role.md 的 body，作为核心身份提示词。
        tools: 声明的工具白名单；None 表示不声明 agent 级白名单。
        memory: 记忆范围；主 agent 默认 ``project``，子 agent 默认不加载。
        enable_thinking: 是否启用模型思考。
        features: 已解析的有效 feature 集（未知名与依赖缺失已处理）。
        requested_model: 未解析的模型请求值（别名或 ``供应商/模型ID``）。
        initial_reasoning_effort: 构造时的推理强度覆盖。
        initial_mode: 构造时的运行模式；运行时权威模式在 `Agent` 自身。
    """

    agent_type: str
    description: str
    is_subagent: bool = False
    role_prompt: str | None = None
    tools: frozenset[str] | None = None
    memory: str | None = None
    enable_thinking: bool = True
    features: frozenset[str] = frozenset()
    requested_model: str | None = None
    initial_reasoning_effort: str | None = None
    initial_mode: RunMode = RunMode.EXECUTE

    @classmethod
    def from_manifest(
        cls,
        manifest: AgentManifest | None,
        *,
        is_subagent: bool = False,
        **overrides: Any,
    ) -> AgentSpec:
        """把 manifest 与调用方覆盖值映射为不可变 spec。

        ``overrides`` 的键名与历史构造参数一致（``model``/``mode``/
        ``reasoning_effort``/``tools``/``features``/``enable_thinking``/
        ``memory``），由本方法映射到 spec 自己的字段名。
        ``manifest`` 为 None 时使用最小回退身份，overrides 仍然生效。

        Args:
            manifest: 解析后的 manifest，或 None。
            is_subagent: 是否为子 agent。
            **overrides: 调用方覆盖值。

        Returns:
            已归一化的不可变 spec。
        """
        values: dict[str, Any] = {
            "agent_type": "agent",
            "description": "",
            "role_prompt": None,
            "tools": None,
            "memory": None,
            "enable_thinking": True,
            "features": None,
            "model": None,
            "reasoning_effort": None,
            "mode": RunMode.EXECUTE,
        }
        if manifest is not None:
            values.update(
                agent_type=manifest.agent_type,
                description=manifest.description,
                role_prompt=manifest.prompt,
                tools=manifest.tools,
                memory=manifest.memory,
                enable_thinking=(
                    manifest.enable_thinking
                    if manifest.enable_thinking is not None
                    else True
                ),
                features=manifest.features,
                model=manifest.model,
                reasoning_effort=manifest.reasoning_effort,
                mode=RunMode.PLAN if manifest.start_in_plan_mode else RunMode.EXECUTE,
            )
        values.update(overrides)

        tools = values["tools"]
        return cls(
            agent_type=values["agent_type"],
            description=values["description"],
            is_subagent=is_subagent,
            role_prompt=values["role_prompt"],
            tools=frozenset(tools) if tools is not None else None,
            memory=resolve_memory_scope(values["memory"], is_subagent),
            enable_thinking=bool(values["enable_thinking"]),
            features=frozenset(resolve_features(values["features"])),
            requested_model=values["model"],
            initial_reasoning_effort=values["reasoning_effort"],
            initial_mode=values["mode"],
        )
