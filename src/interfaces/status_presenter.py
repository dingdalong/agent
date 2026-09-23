"""Pure Rich Text presentation for shared agent and session status."""

from __future__ import annotations

import time
from collections.abc import Sequence

from rich.text import Text

from src.interfaces.agent_view_store import (
    AgentSnapshot,
    ContextUsage,
    SessionSnapshot,
    TokenUsage,
)

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_QUEUED_PREVIEW_MIN_WIDTH = 16
_QUEUED_PREVIEW_LIMIT = 3


def present_queued_inputs(
    texts: Sequence[str],
    *,
    width: int,
    limit: int = _QUEUED_PREVIEW_LIMIT,
) -> Text:
    """渲染输入框上方的排队分组：标题行 + 逐条摘要 + 溢出提示。

    Args:
        texts: 按 FIFO 排列的排队输入原文；每条取首个非空行并折叠空白作为摘要。
        width: 终端宽度，用于截断单条摘要。
        limit: 最多展示的条数，超出时以「… 另有 N 条」收尾。

    Returns:
        Rich Text；无排队内容时为空。
    """
    if not texts:
        return Text()
    lines = [
        f"• 已排队 {len(texts)} 条（下一次模型调用前带上 · Alt+↑ 取回最后一条）"
    ]
    clip_width = max(_QUEUED_PREVIEW_MIN_WIDTH, width - 8)
    for text in texts[:limit]:
        lines.append(f"  ↳ {_clip_queued_preview(_queued_preview_text(text), clip_width)}")
    if len(texts) > limit:
        lines.append(f"  … 另有 {len(texts) - limit} 条")
    return Text("\n".join(lines))


def _queued_preview_text(text: str) -> str:
    """取排队输入的首个非空行并折叠内部空白，作为单行摘要。"""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return " ".join(first.split())


def _clip_queued_preview(preview: str, clip_width: int) -> str:
    """按可见字符截断摘要，超出部分以省略号结尾。"""
    if len(preview) <= clip_width:
        return preview
    return preview[:clip_width] + "…"


def present_spinner_frame(now: float, interval: float = 0.1) -> str:
    """按固定间隔在 spinner 帧之间轮转。

    Args:
        now: 单调时间戳（秒）。
        interval: 每帧保持的秒数，非正值按 0.001 处理。

    Returns:
        当前帧字符。
    """
    return _SPINNER[int(now / max(0.001, interval)) % len(_SPINNER)]


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


def _agent_status_label(snapshot: AgentSnapshot) -> str:
    """构建 agent 状态槽文案：运行中显示实时活动，已结束显示「已完成」。

    Args:
        snapshot: 不可变 agent 视图快照。

    Returns:
        运行中返回其实时活动（思考中/回应中/工具名），无活动时回退「运行中」；
        非运行中返回「已完成」（先判 running，避免显示结束后残留的陈旧活动）。
    """
    if not snapshot.running:
        return "已完成"
    return snapshot.activity or "运行中"


def agent_label(agent_type: str | None, uuid: str | None) -> str:
    """构建跨 TUI 与非 TTY 统一的 agent 身份标签。

    Args:
        agent_type: agent 类型；None 或空串表示无身份。
        uuid: agent 实例 uuid，只取首段作为短标识。

    Returns:
        ``"type short"``；无 uuid 时为 ``"type"``；无 agent_type 时为空串。
    """
    if not agent_type:
        return ""
    short_uuid = uuid.split("-")[0] if uuid else ""
    return f"{agent_type} {short_uuid}" if short_uuid else agent_type


def present_agent_identity(
    snapshot: AgentSnapshot,
    base_style: str = "",
    show_status: bool = True,
    *,
    now: float | None = None,
) -> Text:
    """Present one agent identity and its status slot.

    Args:
        snapshot: Immutable agent view snapshot.
        base_style: Optional Rich style applied to the whole line.
        show_status: 为真时在身份后追加状态槽（实时活动 / 已完成）；为假时仅身份，不含任何状态词。
        now: 可选单调时间戳，用于让同一批状态行使用相同 spinner 帧。

    Returns:
        Rich text containing identity, optionally followed by the status slot.
    """
    # 状态图标：运行中用 spinner 动画帧，已完成用 ✔
    if snapshot.running:
        render_time = time.monotonic() if now is None else now
        frame = present_spinner_frame(render_time)
        icon = f"{frame} "
    else:
        icon = "✔ "

    # 身份 + 任务描述（替代短 UUID + 状态文字）
    task_label = snapshot.task or ""
    if task_label:
        text = Text(f"{icon}{snapshot.agent_type}  {task_label}", style=base_style)
    else:
        # 无任务描述时回退到短 UUID
        short_uuid = snapshot.uuid.split("-")[0] if snapshot.uuid else ""
        text = Text(f"{icon}{snapshot.agent_type}  {short_uuid}", style=base_style)
    if show_status and not snapshot.task:
        text.append(f"  {_agent_status_label(snapshot)}", style=base_style)
    return text


def present_agent(
    snapshot: AgentSnapshot,
    base_style: str = "",
    *,
    now: float | None = None,
) -> Text:
    """Present one agent summary for live, history, and status use.

    Args:
        snapshot: Immutable agent view snapshot.
        base_style: Optional Rich style applied to the whole line.
        now: 可选单调时间戳，用于让同一批状态行使用相同 spinner 帧。

    Returns:
        Rich text containing identity, the status slot（实时活动 / 已完成）, and canonical metrics.
    """
    line = present_agent_identity(snapshot, base_style, now=now)
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
    return Text(f"✔ {short_uuid}  已结束", style=base_style)


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


def present_task_panel(
    tasks: list[dict],
    max_pending_visible: int = 3,
    max_completed_visible: int = 1,
) -> list[str]:
    """渲染任务进度面板行（汇总行 + 逐项列表）。

    Args:
        tasks: AgentViewStore.task_snapshot() 返回的任务摘要字典列表。
        max_pending_visible: 最多显示的待处理任务数，超出折叠。
        max_completed_visible: 最多显示的已完成任务数，超出折叠。

    Returns:
        带缩进的展示行列表。空任务列表时返回空列表。
    """
    if not tasks:
        return []

    in_progress = [t for t in tasks if t["status"] == "in_progress"]
    pending = [t for t in tasks if t["status"] == "pending"]
    completed = [t for t in tasks if t["status"] == "completed"]

    # 汇总行
    total = len(tasks)
    parts: list[str] = []
    if completed:
        parts.append(f"{len(completed)} 已完成")
    if in_progress:
        parts.append(f"{len(in_progress)} 进行中")
    if pending:
        parts.append(f"{len(pending)} 待处理")
    lines = [f"    {total} 任务 ({', '.join(parts)})"]

    # 明细行：in_progress → pending → completed
    for t in in_progress:
        lines.append(f"    ◼ {t['subject']}")

    if len(pending) <= max_pending_visible:
        for t in pending:
            lines.append(f"    ◻ {t['subject']}")
    else:
        for t in pending[:max_pending_visible]:
            lines.append(f"    ◻ {t['subject']}")
        lines.append(f"    … {len(pending) - max_pending_visible} 待处理")

    if len(completed) <= max_completed_visible:
        for t in completed:
            lines.append(f"    ✔ {t['subject']}")
    else:
        for t in completed[:max_completed_visible]:
            lines.append(f"    ✔ {t['subject']}")
        lines.append(f"    … {len(completed) - max_completed_visible} 已完成")

    return lines
