"""斜杠命令 /models — 选择并切换当前 Agent 的模型与推理强度。"""

from __future__ import annotations

import asyncio

from src.commands import command
from src.commands.context import CommandContext
from src.llm.base import normalize_reasoning_effort

_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _alias_labels(llm) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = {}
    for alias in ("default", "best", "fast"):
        labels.setdefault(llm.resolve_model(alias), []).append(alias)
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


def _selection_indexes(agent, models: list[str]) -> tuple[int, int]:
    current_model = getattr(getattr(agent, "llm", None), "model", None)
    model_index = models.index(current_model) if current_model in models else 0
    current_effort = getattr(agent, "reasoning_effort", None) or getattr(
        getattr(agent, "llm", None), "reasoning_effort", "max"
    )
    effort = normalize_reasoning_effort(str(current_effort)) or "max"
    return model_index, _EFFORTS.index(effort)


def _persist_selection(ctx: CommandContext, model: str, effort: str) -> bool:
    role_mgr = getattr(ctx.deps, "role_mgr", None)
    config_mgr = getattr(ctx.deps, "config_mgr", None)
    role_name = getattr(role_mgr, "role_name", None)
    if config_mgr is None or not role_name:
        return False
    config_mgr.set_configs(
        {
            f"role.{role_name}.model": model,
            f"role.{role_name}.reasoning_effort": effort,
        },
        "project",
    )
    config_mgr.reload()
    return True


@command("选择并切换当前 Agent 的模型与推理强度")
async def models(ctx: CommandContext, args: list[str]) -> None:
    """交互选择模型并原地更新 Agent；无交互入口时输出模型列表。"""
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

    model_index, effort_index = _selection_indexes(ctx.agent, available)
    labels = _alias_labels(llm)
    options = [
        (
            model,
            f"{llm.provider_name_for_model(model)}/{model}"
            + (f" [{', '.join(labels[model])}]" if model in labels else ""),
        )
        for model in available
    ]
    selected_model, selected_effort = await request_selection(
        "",
        options,
        _EFFORTS,
        model_index,
        effort_index,
        source="models",
    )
    if not selected_model or not selected_effort:
        return
    if selected_model not in available or selected_effort not in _EFFORTS:
        await ctx.deps.event_bus.request_output("模型选择无效，未应用更改。\n")
        return

    # 先验证并持久化，确保配置失败时当前 Agent 不发生部分切换。
    try:
        llm.get(selected_model)
        persisted = await asyncio.to_thread(
            _persist_selection, ctx, selected_model, selected_effort
        )
        ctx.agent.switch_model(selected_model, selected_effort)
    except Exception as exc:
        await ctx.deps.event_bus.request_output(f"模型切换失败：{exc}\n")
        return

    suffix = "" if persisted else "（未找到活动角色，未写入项目配置）"
    await ctx.deps.event_bus.request_output(
        f"已切换模型：{selected_model}，推理强度：{selected_effort}"
        f"（已保留当前会话历史）。{suffix}\n"
    )
