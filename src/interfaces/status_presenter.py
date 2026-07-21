"""Pure Rich Text presentation for shared agent and session status."""

from __future__ import annotations

from rich.text import Text

from src.interfaces.agent_view_store import (
    AgentSnapshot,
    ContextUsage,
    SessionSnapshot,
    TokenUsage,
)


def format_token_count(count: int) -> str:
    """把 token 数量格式化为共享的紧凑表示。

    Args:
        count: 非负 token 数量。

    Returns:
        小于 1000 时返回精确整数，否则返回按 half-up 取整的 ``m`` 与 ``k`` 文本。
    """
    if count < 1000:
        return str(count)
    rounded_count = ((count + 50) // 100) * 100
    millions, remainder = divmod(rounded_count, 1_000_000)
    remainder_tenths = remainder // 100
    remainder_text = f"{remainder_tenths // 10}.{remainder_tenths % 10}k"
    if millions:
        return f"{millions}m{remainder_text}"
    return remainder_text


def format_elapsed_time(elapsed_seconds: float) -> str:
    """把耗时秒数格式化为整数 ``h``、``m``、``s`` 分段。

    Args:
        elapsed_seconds: 原始耗时秒数，负数按零处理。

    Returns:
        按 half-up 取整，并在出现高位单位后保留低位单位的耗时文本。
    """
    nonnegative_seconds = max(0.0, elapsed_seconds)
    whole_seconds = int(nonnegative_seconds)
    fraction = nonnegative_seconds - whole_seconds
    rounded_seconds = whole_seconds + int(fraction >= 0.5)
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def present_session_metrics(
    snapshot: SessionSnapshot,
    elapsed_seconds: float,
    base_style: str = "",
) -> Text:
    """Present session tokens with foreground context and elapsed time.

    Args:
        snapshot: Session totals and foreground context.
        elapsed_seconds: Elapsed time displayed at the end of the metric line.
        base_style: Optional Rich style applied to the whole line.

    Returns:
        Rich text using the canonical metrics format.
    """
    return _present_metrics(
        snapshot.usage,
        snapshot.foreground_context,
        elapsed_seconds,
        base_style,
    )


def present_agent_identity(snapshot: AgentSnapshot, base_style: str = "") -> Text:
    """Present one agent identity and lifecycle state.

    Args:
        snapshot: Immutable agent view snapshot.
        base_style: Optional Rich style applied to the whole line.

    Returns:
        Rich text containing identity and lifecycle state.
    """
    status = "运行中" if snapshot.running else "已完成"
    short_uuid = snapshot.uuid.split("-")[0] if snapshot.uuid else ""
    return Text(f"◯ {snapshot.agent_type}  {short_uuid}  {status}", style=base_style)


def present_agent(snapshot: AgentSnapshot, base_style: str = "") -> Text:
    """Present one agent summary for live, history, and status use.

    Args:
        snapshot: Immutable agent view snapshot.
        base_style: Optional Rich style applied to the whole line.

    Returns:
        Rich text containing identity, lifecycle state, and canonical metrics.
    """
    line = present_agent_identity(snapshot, base_style)
    line.append("  ", style=base_style)
    line.append_text(_present_metrics(
        snapshot.usage,
        snapshot.context,
        snapshot.elapsed_seconds,
        base_style,
    ))
    return line


def present_ended_agent(agent_uuid: str, base_style: str = "") -> Text:
    """Present a retained-view fallback for an unavailable agent snapshot.

    Args:
        agent_uuid: Agent UUID whose snapshot is no longer retained.
        base_style: Optional Rich style applied to the whole line.

    Returns:
        Rich text containing the short UUID and ended state.
    """
    short_uuid = agent_uuid.split("-")[0] if agent_uuid else ""
    return Text(f"◯ {short_uuid}  已结束", style=base_style)


def _present_metrics(
    usage: TokenUsage,
    context: ContextUsage,
    elapsed_seconds: float,
    base_style: str,
) -> Text:
    """构建所有状态展示共用的规范指标段。

    Args:
        usage: 当前展示范围的累计 token 用量。
        context: 当前展示 agent 最新的精确上下文用量。
        elapsed_seconds: 将被格式化为整数时间分段的耗时秒数。
        base_style: 应用于整个指标段的可选 Rich 样式。

    Returns:
        Rich Text 指标段。
    """
    line = Text(style=base_style)
    cache_pct = (
        usage.cache_read_tokens / usage.input_tokens * 100
        if usage.input_tokens
        else 0.0
    )
    line.append(f"↑{format_token_count(usage.input_tokens)}")
    line.append(f"({cache_pct:.0f}%)", style=_combine_styles(base_style, "bright_black"))
    line.append(f" ↓{format_token_count(usage.output_tokens)}")
    line.append(" · ", style=_combine_styles(base_style, "bright_black"))
    _append_context(line, context, base_style)
    line.append(" · ", style=_combine_styles(base_style, "bright_black"))
    line.append(format_elapsed_time(elapsed_seconds))
    return line


def _append_context(line: Text, context: ContextUsage, base_style: str) -> None:
    """Append context use and an optional warning-colored percentage.

    Args:
        line: Rich text mutated in place.
        context: Used and limit token values.
        base_style: Optional style applied to all appended text.

    Returns:
        None.
    """
    line.append(f"上下文 {format_token_count(context.used_tokens)}", style=base_style)
    if context.limit_tokens <= 0:
        return
    pct = context.used_tokens / context.limit_tokens * 100
    if pct >= 90:
        warning_style = "red"
    elif pct >= 80:
        warning_style = "yellow"
    else:
        warning_style = "bright_black"
    line.append(f"({pct:.0f}%)", style=_combine_styles(base_style, warning_style))


def _combine_styles(base_style: str, added_style: str) -> str:
    """Combine an optional base Rich style with one semantic style.

    Args:
        base_style: Optional existing Rich style.
        added_style: Semantic style appended to the base.

    Returns:
        Space-separated Rich style string.
    """
    return f"{base_style} {added_style}".strip()
