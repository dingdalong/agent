"""斜杠命令 /models — 按 provider 分组列出已发现模型并标注 default/best/fast。"""

from __future__ import annotations

from src.commands import command
from src.commands.context import CommandContext


@command("查看支持的模型列表")
async def models(ctx: CommandContext, args: list[str]) -> None:
    """列出已发现模型并反向标注配置别名。"""
    llm = ctx.deps.llm_mgr
    grouped = llm.models_by_provider()

    # 解析配置别名到真实模型 ID 并反向标注；best/fast 缺省或与 default 相同时合并到同一模型
    labels: dict[str, list[str]] = {}
    for alias in ("default", "best", "fast"):
        labels.setdefault(llm.resolve_model(alias), []).append(alias)

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

    await ctx.deps.event_bus.request_output("\n".join(lines) + "\n")
