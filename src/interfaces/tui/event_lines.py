"""事件与会话记录到展示块的唯一投影。

实时 TTY、非 TTY 兜底与会话回放共用这里的构建函数，保证同一事实只有一份措辞；样式随块
返回，非 TTY 只取 `plain` 文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from rich.text import Text

from src.common.session_state import SessionRecord
from src.common.tool_display import format_params, format_result, permission_line, result_preview
from src.events.types import (
    CompactDelta,
    LLMCallFailed,
    LLMLengthRetrying,
    LLMRetrying,
    PermissionNotice,
)
from src.interfaces.status_presenter import agent_label


THINKING_CONTENT_STYLE = "#8d989f"
_USER_CONTENT_STYLE = "#76d7c4"
_LABEL_STYLE = "bold #76d7c4"
_RETRY_STYLE = "yellow"
_FAILURE_STYLE = "red"
_COMPACT_STYLE = "bright_black"
_PERMISSION_ALLOW_STYLE = "#efc36a"
_PERMISSION_DENY_STYLE = "#ff6b6b"

_TRUNCATION_KINDS = {
    "tool_call": "工具调用",
    "content": "正文",
    "thinking": "思考",
    "unknown": "未知",
}


@dataclass(frozen=True, slots=True)
class DisplayBlock:
    """载体无关的展示块：TTY 保留样式，非 TTY 只取 `plain`。"""

    content: str | Text
    markdown: bool = False
    style: str | None = None
    spacing: int = 1

    @property
    def plain(self) -> str:
        return self.content.plain if isinstance(self.content, Text) else self.content


@dataclass(slots=True)
class RoundEntry:
    """本轮的一次工具调用，包含开始与完成的展示信息。"""

    tool_call_id: str
    tool_name: str
    detail: str
    started_monotonic: float
    status: str = "running"
    preview: str = ""
    duration: float = 0.0
    start_display: object | None = None   # ToolDisplay
    result_display: object | None = None  # ToolDisplay


def user_block(text: str) -> DisplayBlock:
    """用户输入块：首行 `›` 前缀，续行缩进对齐。"""
    lines = text.split("\n")
    rendered = "\n".join(
        f"› {line}" if index == 0 else f"  {line}"
        for index, line in enumerate(lines)
    )
    return DisplayBlock(rendered, style=_USER_CONTENT_STYLE)


def stream_label(kind: str, agent_type: str | None, uuid: str | None) -> str:
    """流式输出标题：子 agent 带身份标签，主 agent 用默认称呼。"""
    label = agent_label(agent_type, uuid)
    if kind == "thinking":
        return f"思考({label})：" if label else "思考"
    return f"回复({label})：" if label else "助手："


def stream_label_block(kind: str, agent_type: str | None, uuid: str | None) -> DisplayBlock:
    """流式输出标题块：`› ` 前缀加身份标签，与紧跟的流式正文紧贴。"""
    return DisplayBlock(
        Text(f"› {stream_label(kind, agent_type, uuid)}", style=_LABEL_STYLE),
        spacing=0,
    )


def retry_block(
    error_kind: str,
    safe_message: str,
    attempt: int,
    max_attempts: int,
    *,
    partial: bool = False,
    tool_fragment_state: str = "",
    countdown: int | None = None,
) -> DisplayBlock | None:
    """可重试失败的永久提示行。

    仅当已输出残片（需要标注这段输出作废）或调用方要求倒计时（非 TTY 无状态栏）
    时产出；其余情况由 TTY 状态栏的倒计时承担提示。
    """
    if not partial and countdown is None:
        return None
    text = f"⚠ 尝试 {attempt}/{max_attempts} 失败，将重试 [{error_kind}] {safe_message}"
    if partial:
        text += f" (partial=true, tool={tool_fragment_state})"
    if countdown is not None:
        text += f"；{countdown}秒后重试"
    return DisplayBlock(Text(text, style=_RETRY_STYLE))


def length_retry_block(
    truncation_kind: str,
    strategy: str,
    effort: str,
    attempt: int,
    max_attempts: int,
) -> DisplayBlock:
    """输出被长度上限截断后自动恢复的永久提示行。"""
    kind = _TRUNCATION_KINDS.get(truncation_kind, truncation_kind)
    if strategy == "regenerate-lower-effort":
        action = f"降低推理力度至 {effort} 后重生成"
    elif strategy == "regenerate-compress":
        action = "压缩思考后重生成"
    else:
        action = "从中断处继续生成"
    return DisplayBlock(
        Text(
            f"⚠ 输出截断（{kind}）：{action} ({attempt}/{max_attempts})",
            style=_RETRY_STYLE,
        )
    )


def call_failed_block(
    error_kind: str,
    safe_message: str,
    *,
    request_id: str = "",
    diagnostic_id: str = "",
) -> DisplayBlock:
    """LLM 调用终态失败的永久提示行。"""
    identifiers = []
    if request_id:
        identifiers.append(f"request_id={request_id}")
    if diagnostic_id:
        identifiers.append(f"diagnostic_id={diagnostic_id}")
    suffix = f" ({', '.join(identifiers)})" if identifiers else ""
    return DisplayBlock(
        Text(f"✘ LLM 调用失败 [{error_kind}] {safe_message}{suffix}", style=_FAILURE_STYLE)
    )


def compact_block(
    content: str,
    agent_type: str | None = None,
    uuid: str | None = None,
) -> DisplayBlock:
    """上下文压缩提示行，带发起 agent 身份。"""
    label = agent_label(agent_type, uuid)
    prefix = f"{label} " if label else ""
    return DisplayBlock(
        Text(f"[compact] {prefix}{content.strip() or 'context'}", style=_COMPACT_STYLE)
    )


def permission_block(
    status: str,
    tool_name: str,
    detail: str,
    decision_source: str,
) -> DisplayBlock:
    """权限结论提示行。"""
    style = _PERMISSION_DENY_STYLE if status == "deny" else _PERMISSION_ALLOW_STYLE
    return DisplayBlock(
        Text(permission_line(status, tool_name, detail or "", decision_source), style=style)
    )


def blocks_for_event(event: object, *, retry_countdown: int | None = None) -> list[DisplayBlock]:
    """实时事件的展示块；`retry_countdown` 仅非 TTY 传入。"""
    if isinstance(event, LLMRetrying):
        block = retry_block(
            event.error_kind,
            event.safe_message,
            event.attempt,
            event.max_attempts,
            partial=event.partial,
            tool_fragment_state=event.tool_fragment_state,
            countdown=retry_countdown,
        )
        return [block] if block is not None else []
    if isinstance(event, LLMLengthRetrying):
        return [length_retry_block(
            event.truncation_kind,
            event.strategy,
            event.effort,
            event.attempt,
            event.max_attempts,
        )]
    if isinstance(event, LLMCallFailed):
        return [call_failed_block(
            event.error_kind,
            event.safe_message,
            request_id=event.request_id,
            diagnostic_id=event.diagnostic_id,
        )]
    if isinstance(event, CompactDelta):
        return [compact_block(
            event.content,
            event.caller_agent_type,
            event.caller_uuid,
        )]
    if isinstance(event, PermissionNotice):
        return [permission_block(
            event.status,
            event.tool_name,
            event.detail,
            event.decision_source,
        )]
    return []


def blocks_for_view(record: SessionRecord, *, resolver: object | None = None) -> list[DisplayBlock]:
    """会话记录回放的展示块，与实时路径共用同一批构建函数。

    Args:
        record: 待渲染的会话记录。
        resolver: 可选的内容解析器（通常是 SessionState），用于把展示配方解析回
            唯一一份源文本；缺省时只显示配方自带的标题与摘要。

    Returns:
        展示块列表。
    """
    view = record.view
    if view is None:
        return []
    data = view.data
    if view.kind == "user":
        return [user_block(str(data.get("text", "")))]
    if view.kind == "output":
        return [DisplayBlock(
            str(data.get("content", "")),
            markdown=bool(data.get("markdown", False)),
        )]
    if view.kind == "assistant":
        return _assistant_blocks(data)
    if view.kind == "tool":
        return [DisplayBlock(round_entry_text(_round_entry_from_view(
            data,
            resolver=resolver,
            tool_call_id=record.correlation_id,
        )))]
    if view.kind == "event":
        return _event_blocks(data)
    return []


def _assistant_blocks(data: dict) -> list[DisplayBlock]:
    agent_type = data.get("caller_agent_type")
    uuid = data.get("caller_uuid")
    blocks: list[DisplayBlock] = []
    thinking = str(data.get("thinking", ""))
    content = str(data.get("content", ""))
    if thinking:
        blocks.append(stream_label_block("thinking", agent_type, uuid))
        blocks.append(DisplayBlock(thinking, markdown=True, style=THINKING_CONTENT_STYLE))
    if content:
        blocks.append(stream_label_block("response", agent_type, uuid))
        blocks.append(DisplayBlock(content, markdown=True))
    event_type = data.get("event_type")
    if event_type == "llm_retrying" and data.get("partial"):
        block = retry_block(
            str(data.get("error_kind", "")),
            str(data.get("safe_message", "")),
            int(data.get("attempt", 0) or 0),
            int(data.get("max_attempts", 0) or 0),
            partial=True,
            tool_fragment_state=str(data.get("tool_fragment_state", "")),
        )
        if block is not None:
            blocks.append(block)
    elif event_type == "llm_length_retrying":
        blocks.append(length_retry_block(
            str(data.get("truncation_kind", "")),
            str(data.get("strategy", "")),
            str(data.get("effort", "")),
            int(data.get("attempt", 0) or 0),
            int(data.get("max_attempts", 0) or 0),
        ))
    elif event_type == "llm_call_failed":
        blocks.append(call_failed_block(
            str(data.get("error_kind", "")),
            str(data.get("safe_message", "")),
            request_id=str(data.get("request_id", "")),
            diagnostic_id=str(data.get("diagnostic_id", "")),
        ))
    return blocks


def _event_blocks(data: dict) -> list[DisplayBlock]:
    event_type = data.get("event_type")
    if event_type == "compact_delta":
        return [compact_block(
            str(data.get("content", "")),
            data.get("caller_agent_type"),
            data.get("caller_uuid"),
        )]
    if event_type == "permission_notice":
        return [permission_block(
            str(data.get("status", "allow")),
            str(data.get("tool_name", "")),
            str(data.get("detail", "")),
            str(data.get("decision_source", "")),
        )]
    return []


def _round_entry_from_view(
    data: dict,
    *,
    resolver: object | None = None,
    tool_call_id: str | None = None,
) -> RoundEntry:
    started = data.get("started", {})
    completed = data.get("completed", {})
    started = started if isinstance(started, dict) else {}
    completed = completed if isinstance(completed, dict) else {}
    tool_name = str(started.get("tool_name") or completed.get("tool_name") or "tool")
    start_display = _display_view(started.get("display"), resolver, tool_call_id, tool_name)
    result_display = _display_view(completed.get("display"), resolver, tool_call_id)
    return RoundEntry(
        tool_name=tool_name,
        tool_call_id=str(
            started.get("tool_call_id") or completed.get("tool_call_id") or tool_call_id or ""
        ),
        detail=str(started.get("detail", "")),
        started_monotonic=0.0,
        status=(
            "success"
            if completed.get("status") == "success"
            else "error" if completed else "running"
        ),
        preview=_preview_text(completed, resolver, tool_call_id),
        duration=float(completed.get("duration_seconds", 0.0) or 0.0),
        start_display=start_display,
        result_display=result_display,
    )


def _display_view(
    value: object,
    resolver: object | None,
    tool_call_id: str | None,
    tool_name: str | None = None,
) -> SimpleNamespace | None:
    """把持久化的展示数据还原成带正文的展示对象。"""
    if not isinstance(value, dict):
        return None
    display = dict(value)
    if not display.get("content") and display.get("render"):
        display["content"] = _render_content(display["render"], resolver, tool_call_id, tool_name)
    return SimpleNamespace(**display)


def _render_content(
    render: object,
    resolver: object | None,
    tool_call_id: str | None,
    tool_name: str | None = None,
) -> str:
    """按展示配方从唯一一份源数据重建正文。"""
    if not isinstance(render, dict) or resolver is None:
        return ""
    if render.get("kind") == "params":
        lookup_args = getattr(resolver, "tool_call_arguments", None)
        arguments = lookup_args(tool_call_id) if lookup_args is not None else None
        if not isinstance(arguments, dict) or not tool_name:
            return ""
        return format_params(tool_name, arguments)
    if render.get("kind") != "result":
        return ""
    lookup = getattr(resolver, "tool_result_text", None)
    if lookup is None:
        return ""
    text = lookup(tool_call_id)
    if not isinstance(text, str) or not text:
        return ""
    return format_result(text)[0]


def _preview_text(
    completed: dict,
    resolver: object | None,
    tool_call_id: str | None,
) -> str:
    """返回工具结果预览：优先用存储值，投影被剥离时按引用重建。"""
    stored = completed.get("result_preview")
    if isinstance(stored, str) and stored:
        return stored
    display = completed.get("display")
    render = display.get("render") if isinstance(display, dict) else None
    if not isinstance(render, dict) or render.get("kind") != "result":
        return ""
    lookup = getattr(resolver, "tool_result_text", None) if resolver is not None else None
    text = lookup(tool_call_id) if lookup is not None else None
    return result_preview(text) if isinstance(text, str) else ""


def round_entry_text(entry: RoundEntry) -> Text:
    """本轮工具调用的多行展示文本（状态行 + 参数 + 结果）。"""
    text = Text()
    if entry.status == "running":
        text.append(f"⋯ {_entry_title(entry)}  进程运行中", style="bright_black")
        return text
    ok = entry.status == "success"
    mark = "✔" if ok else "✘"
    text.append(f"{mark} {_entry_title(entry)}", style="green" if ok else "red")
    text.append(f"  ({entry.duration:.2f}s)", style="bright_black")
    start_content = _display_content(entry.start_display)
    if start_content:
        text.append("\n")
        for line in start_content.splitlines():
            text.append(f"  {line}\n", style="bright_black")
    result_display = entry.result_display
    if result_display is not None and hasattr(result_display, "content_type"):
        if result_display.content_type == "diff" and result_display.content:
            text.append("  ─────\n", style="bright_black")
            for line in result_display.content.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("+ "):
                    text.append(f"{line}\n", style="green on #1a2e1a")
                elif stripped.startswith("- "):
                    text.append(f"{line}\n", style="red on #2e1a1a")
                else:
                    text.append(f"{line}\n", style="bright_black")
        elif result_display.content:
            text.append("  ─────\n", style="bright_black")
            for line in result_display.content.splitlines()[:20]:
                text.append(f"  {line}\n", style="bright_black" if ok else "red")
    elif not ok:
        preview_lines = entry.preview.splitlines()
        if preview_lines:
            text.append("\n")
            for line in preview_lines[:5]:
                text.append(f"  {line}\n", style="red")
    return text


def _entry_title(entry: RoundEntry) -> str:
    """工具条目标题：优先用 display 标题，回退工具名 + 参数摘要。"""
    for display in (entry.result_display, entry.start_display):
        if display is not None:
            title = getattr(display, "title", "") or ""
            if title:
                return title
    detail = f" {entry.detail}" if entry.detail else ""
    return f"{entry.tool_name}{detail}"


def _display_content(display: object | None) -> str:
    """提取 ToolDisplay 的内容文本。"""
    if display is None or not hasattr(display, "content"):
        return ""
    return (display.content or "").strip()
