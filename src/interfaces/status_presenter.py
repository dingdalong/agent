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
    """Format a token count in the compact shared representation.

    Args:
        count: Nonnegative token count.

    Returns:
        Decimal text below 1000, otherwise one-decimal ``k`` text.
    """
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


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
    """Build the canonical metrics segment shared by all status surfaces.

    Args:
        usage: Cumulative token usage for the displayed scope.
        context: Latest exact context use for the displayed agent.
        elapsed_seconds: Elapsed time shown in seconds.
        base_style: Optional Rich style applied to the whole segment.

    Returns:
        Rich text metrics segment.
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
    line.append(f"{max(0.0, elapsed_seconds):.1f}s")
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
