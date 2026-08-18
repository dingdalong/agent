"""斜杠命令 /models — 选择并切换当前角色的两个模型槽位与推理强度。"""

from __future__ import annotations

import asyncio

from src.commands import command
from src.commands.context import CommandContext
from src.llm.base import normalize_reasoning_effort
from src.llm.errors import LLMConfigurationError
from src.mgr.llm_mgr import ModelUnavailableError
from src.mgr.role_mgr import format_role_config_key

_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
_SLOTS = ("default", "fast")


def _alias_labels(llm) -> dict[str, list[str]]:
    """把两个槽位当前解析到的模型映射为槽位标签列表。

    Args:
        llm: LLMMgr 实例。

    Returns:
        {模型ID: [槽位名, ...]}；槽位配置非法或模型不可用时跳过该槽位，
        保证纯文本列表在无可用模型时仍能降级输出。
    """
    labels: dict[str, list[str]] = {}
    for alias in _SLOTS:
        try:
            model = llm.resolve_model(alias)
        except (ModelUnavailableError, LLMConfigurationError):
            continue
        labels.setdefault(model, []).append(alias)
    return labels


def _model_listing(llm) -> str:
    grouped = llm.models_by_provider()
    labels = _alias_labels(llm)

    lines: list[str] = []
    if grouped:
        lines.append("支持的模型（按 provider 分组）:")
        for provider, models in grouped.items():
            lines.append("")
            lines.append(f"{provider}:")
            for model in models:
                suffix = f" [{', '.join(labels[model])}]" if model in labels else ""
                lines.append(f"  - {model}{suffix}")
    else:
        lines.append("当前没有可用模型。")
    return "\n".join(lines) + "\n"


def _slot_index(llm, alias: str, models: list[str]) -> int:
    """定位某槽位当前模型在可用模型列表中的下标。

    Args:
        llm: LLMMgr 实例。
        alias: 槽位名（default/fast）。
        models: 可用模型列表。

    Returns:
        槽位当前模型的下标；槽位不可解析或模型不在列表中时回退 0。
    """
    try:
        model = llm.resolve_model(alias)
    except (ModelUnavailableError, LLMConfigurationError):
        return 0
    return models.index(model) if model in models else 0


def _effective_reasoning_effort(agent) -> str:
    """返回当前 agent 实际使用的规范化推理强度。

    Args:
        agent: 当前 agent；未显式设置时从其 LLM provider 回退。

    Returns:
        规范化后的推理强度；现值非法或缺失时回退 ``max``。
    """
    current = getattr(agent, "reasoning_effort", None) or getattr(
        getattr(agent, "llm", None), "reasoning_effort", "max"
    )
    return normalize_reasoning_effort(str(current)) or "max"


def _selection_indexes(agent, llm, models: list[str]) -> tuple[int, int, int]:
    """给出三轴菜单的初始下标。

    Args:
        agent: 当前 agent（提供推理强度现值）。
        llm: LLMMgr 实例（提供两个槽位现值）。
        models: 可用模型列表。

    Returns:
        (default 槽位下标, fast 槽位下标, 推理强度下标)。
    """
    effort = _effective_reasoning_effort(agent)
    return (
        _slot_index(llm, "default", models),
        _slot_index(llm, "fast", models),
        _EFFORTS.index(effort),
    )


def _persist_selection(
    ctx: CommandContext, default_model: str, fast_model: str, effort: str
) -> bool:
    """把两个槽位与推理强度写回项目层角色配置。

    模型槽位整体写父键 ``role.<角色>.model`` 的 mapping（而非点路径子键），
    项目层残留旧标量 model 值时也能直接抹平。

    Args:
        ctx: 命令上下文。
        default_model: default 槽位模型。
        fast_model: fast 槽位模型。
        effort: 角色级推理强度。

    Returns:
        写入成功为 True；缺少 config_mgr 或活动角色名时为 False。
    """
    role_mgr = getattr(ctx.deps, "role_mgr", None)
    config_mgr = getattr(ctx.deps, "config_mgr", None)
    role_name = getattr(role_mgr, "role_name", None)
    if config_mgr is None or not role_name:
        return False
    config_mgr.set_configs_parts(
        {
            ("role", role_name, "model"): {
                "default": default_model,
                "fast": fast_model,
            },
            ("role", role_name, "reasoning_effort"): effort,
        },
        "project",
    )
    config_mgr.reload()
    return True


def _untrusted_notice(ctx: CommandContext) -> str:
    """项目未信任时的拒绝提示。

    Args:
        ctx: 命令上下文。

    Returns:
        说明为何不生效、以及改哪里才生效的提示文本。
    """
    config_mgr = getattr(ctx.deps, "config_mgr", None)
    role_name = getattr(getattr(ctx.deps, "role_mgr", None), "role_name", None) or "<角色>"
    global_path = getattr(config_mgr, "global_config_path", None) or "全局 config.yaml"
    return (
        "当前项目未被信任，项目层模型配置会被忽略，已取消本次切换（未写配置、未切换模型）。\n"
        f"请先信任该项目后重试，或在全局配置 {global_path} 中配置 "
        f"{format_role_config_key(role_name, 'model')} 的 default/fast 槽位与 "
        f"{format_role_config_key(role_name, 'reasoning_effort')}。\n"
    )


@command("选择并切换当前角色的模型槽位与推理强度")
async def models(ctx: CommandContext, args: list[str]) -> None:
    """交互选择两个模型槽位与推理强度；无交互入口时输出模型列表。"""
    del args
    llm = ctx.deps.llm_mgr
    available = llm.list_models()
    if not available or ctx.agent is None:
        await ctx.deps.event_bus.request_output(_model_listing(llm))
        return

    request_selection = getattr(ctx.deps.event_bus, "request_model_selection", None)
    if not callable(request_selection):
        await ctx.deps.event_bus.request_output(_model_listing(llm))
        return

    config_mgr = getattr(ctx.deps, "config_mgr", None)
    role_name = getattr(getattr(ctx.deps, "role_mgr", None), "role_name", None)
    if config_mgr is None or not role_name:
        await ctx.deps.event_bus.request_output(
            "当前无法更新模型：配置管理器不可用或未找到活动角色，"
            "已取消本次切换（未写配置、未切换模型）。\n"
        )
        return

    # 未信任项目的项目层 role.*.model 会在 reload 时被剥离，写了也不生效，
    # 直接拒绝，避免「当前 agent 已切换、新建子 agent 仍用旧模型」的不一致状态。
    if not getattr(config_mgr, "project_trusted", False):
        await ctx.deps.event_bus.request_output(_untrusted_notice(ctx))
        return

    default_index, fast_index, effort_index = _selection_indexes(ctx.agent, llm, available)
    options = [(model, f"{llm.provider_name_for_model(model)}/{model}") for model in available]
    default_model, fast_model, effort = await request_selection(
        "",
        options,
        _EFFORTS,
        default_index,
        fast_index,
        effort_index,
        source="models",
    )
    if not default_model or not fast_model or not effort:
        return
    if default_model not in available or fast_model not in available or effort not in _EFFORTS:
        await ctx.deps.event_bus.request_output("模型选择无效，未应用更改。\n")
        return

    current_model = getattr(getattr(ctx.agent, "llm", None), "model", None)
    current_effort = _effective_reasoning_effort(ctx.agent)
    switch_agent = default_model != current_model or effort != current_effort

    # 先验证两个槽位都能实例化并持久化，确保任一步失败时整体不生效。
    try:
        llm.get(default_model)
        llm.get(fast_model)
        persisted = await asyncio.to_thread(
            _persist_selection, ctx, default_model, fast_model, effort
        )
        if not persisted:
            await ctx.deps.event_bus.request_output(
                "模型切换失败：配置管理器不可用或未找到活动角色，"
                "未写配置、未切换模型。\n"
            )
            return
        # fast 槽位只被新建子 agent 与智能权限裁决消费，它们每次现读配置，无需热切。
        if switch_agent:
            ctx.agent.switch_model(default_model, effort)
    except Exception as exc:
        await ctx.deps.event_bus.request_output(f"模型切换失败：{exc}\n")
        return

    detail = (
        "当前 agent 已切换（已保留当前会话历史）。"
        if switch_agent
        else "当前 agent 保持不变，新建子 agent 与智能权限裁决将使用新的 fast 槽位。"
    )
    await ctx.deps.event_bus.request_output(
        f"已更新模型槽位：default={default_model}，fast={fast_model}，"
        f"推理强度：{effort}。{detail}\n"
    )
