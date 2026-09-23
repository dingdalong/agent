"""TUI chrome 渲染器：活动区、回合状态行、输入状态行与分隔线。

ChromeRenderer 只读消费 AgentTuiApp 的回合字段与 AgentViewStore 视图快照，
按段缓存渲染结果，避免每帧重复组装并重绘静态内容。状态权威写入者仍是
App（回合字段）与 AgentViewStore（快照）；App 通过 refresh_chrome 与
周期 tick 委托本渲染器刷新状态区。
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from rich.text import Text

from src.interfaces.status_presenter import (
    format_elapsed_time,
    format_token_count,
    present_agent,
    present_ended_agent,
    present_queued_inputs,
    present_session_metrics,
    present_spinner_frame,
    present_task_panel,
)

if TYPE_CHECKING:
    from src.interfaces.tui.app import AgentTuiApp

_ROUND_PANEL_MAX_ROWS = 8


def rich_text_signature(text: Text) -> tuple[Any, ...]:
    """返回覆盖 Rich Text 可见内容和样式的稳定签名。"""
    return text.plain, text.style, tuple(text.spans)


class ChromeRenderer:
    """渲染 App 四周的常驻状态区，并按内容签名跳过无变化的重绘。"""

    def __init__(self, app: AgentTuiApp) -> None:
        self._app = app
        self._dirty = False
        self._activity_line_count = 0
        self._activity_content_cache: str | None = None
        self._status_signature: tuple[Any, ...] | None = None
        self._input_status_cache: str | None = None
        self._queued_signature: tuple[Any, ...] | None = None
        self._separator_width = 0

    @property
    def dirty(self) -> bool:
        """是否有待合并刷新的事件更新。"""
        return self._dirty

    def mark_dirty(self) -> None:
        """标记 chrome 需要刷新，由下一次 tick 合并执行。"""
        self._dirty = True

    def update(self) -> None:
        """全量渲染四段状态区；调用方须保证 App 已挂载。"""
        self.render_queued_inputs()
        self.render_activity()
        self.render_status()
        self.render_input_status()
        self._dirty = False

    def update_dynamic(self) -> None:
        """只重绘会随时间推进的两段，供周期 tick 使用。"""
        self.render_activity()
        self.render_status()

    def is_dynamic(self) -> bool:
        """判断当前是否仍有需要逐帧推进的 chrome 内容。"""
        app = self._app
        if app.transcript.viewing_agent_id:
            snapshot = app.agent_view_store.agent_snapshot(
                app.transcript.viewing_agent_id
            )
            return bool(snapshot is not None and snapshot.running)
        if app.coordinator.input_active:
            return app._has_sub_agents()
        return bool(
            app._activity
            or app._round_entries
            or app._retry_deadline is not None
            or app._turn_started is not None
            or app._has_sub_agents()
        )

    def update_separators(self) -> None:
        """按终端宽度重画上下分隔线；宽度未变时跳过。"""
        app = self._app
        if not hasattr(app, "_separator_top"):
            return
        width = max(1, app.screen.size.width)
        if width == self._separator_width:
            return
        self._separator_width = width
        separator = "─" * width
        app._separator_top.update(separator, layout=False)
        app._separator_bottom.update(separator, layout=False)

    def render_activity(self) -> None:
        """渲染活动区：重试倒计时 / 本轮工具面板 / 进行中活动 + 任务进度。"""
        app = self._app
        task_snapshot = app.agent_view_store.task_snapshot()
        visible = (
            not app.coordinator.input_active
            and not app.transcript.viewing_agent_id
            and not app.coordinator.inline_widget_active
            and bool(
                app._activity
                or app._round_entries
                or app._retry_deadline is not None
                or task_snapshot
            )
        )
        app._activity_widget.display = visible
        if not visible:
            return
        now = time.monotonic()
        frame = present_spinner_frame(now, app.render_policy.activity_interval)

        if app._retry_deadline is not None:
            remaining = max(0, math.ceil(app._retry_deadline - now))
            base_content = (
                f"{frame} {app._active_agent_name()} · "
                f"LLM错误[{app._retry_error_kind}] {app._retry_safe_message}，"
                f"{remaining}秒后重试 ({app._retry_attempt}/{app._retry_max})"
            )
        elif app._round_entries:
            entries = app._round_entries
            done = sum(entry.status != "running" for entry in entries)
            lines = [
                f"{frame} 本轮 · {len(entries)} 工具"
                f"（{done} 完成 · {len(entries) - done} 进行中）"
            ]
            for entry in entries[:_ROUND_PANEL_MAX_ROWS]:
                if entry.status == "running":
                    mark = frame
                    elapsed = max(0.0, now - entry.started_monotonic)
                else:
                    mark = "✔" if entry.status == "success" else "✘"
                    elapsed = entry.duration
                detail = f" {entry.detail}" if entry.detail else ""
                start_display = entry.start_display
                if start_display is not None and hasattr(start_display, "title"):
                    title = start_display.title
                    content = (getattr(start_display, "content", "") or "").strip()
                    if content:
                        first_line = content.splitlines()[0]
                        if len(first_line) > 60:
                            first_line = first_line[:60] + "…"
                        title = f"{title}  {first_line}"
                else:
                    title = f"{entry.tool_name}{detail}"
                lines.append(f"  {mark} {title} ({format_elapsed_time(elapsed)})")
            hidden = len(entries) - _ROUND_PANEL_MAX_ROWS
            if hidden > 0:
                lines.append(f"  … 还有 {hidden} 个")
            base_content = "\n".join(lines)
        elif app._activity:
            # 有 in_progress 任务时，spinner 行使用任务描述 + 任务耗时 + token 信息
            ip_task = next(
                (task for task in task_snapshot if task["status"] == "in_progress"),
                None,
            )
            if ip_task:
                spinner_text = ip_task.get("active_form") or ip_task["subject"]
                task_started = ip_task.get("started_monotonic")
                task_elapsed = (
                    format_elapsed_time(now - task_started) if task_started else ""
                )
                usage = app.agent_view_store.session_snapshot().usage
                cache_pct = (
                    usage.cache_read_tokens / usage.input_tokens * 100
                    if usage.input_tokens
                    else 0.0
                )
                tokens = format_token_count(usage.output_tokens)
                token_info = f"{tokens}({cache_pct:.0f}%)"
                elapsed_part = f"{task_elapsed} · " if task_elapsed else ""
                base_content = f"{frame} {spinner_text}… ({elapsed_part}{token_info})"
            else:
                base_content = (
                    f"{frame} {app._active_agent_name()} · {app._activity} "
                    f"({format_elapsed_time(app._activity_elapsed(now))})"
                )
        else:
            base_content = ""

        task_lines = present_task_panel(task_snapshot)
        if task_lines and base_content:
            full_content = base_content + "\n" + "\n".join(task_lines)
        elif task_lines:
            full_content = "\n".join(task_lines)
        else:
            full_content = base_content

        line_count = full_content.count("\n") + 1 if full_content else 0
        layout = line_count != self._activity_line_count
        self._activity_line_count = line_count
        if full_content == self._activity_content_cache:
            return
        app._activity_widget.update(full_content, layout=layout)
        self._activity_content_cache = full_content

    def render_status(self) -> None:
        """渲染底部状态行：模式、会话指标、中断/查看提示与待处理交互。"""
        app = self._app
        now = time.monotonic()
        if app.transcript.viewing_agent_id:
            snapshot = app.agent_view_store.agent_snapshot(
                app.transcript.viewing_agent_id
            )
            status = (
                present_agent(snapshot)
                if snapshot is not None
                else present_ended_agent(app.transcript.viewing_agent_id)
            )
        else:
            session_elapsed = app.agent_view_store.session_elapsed
            if app._turn_started is not None and not app.coordinator.input_active:
                elapsed = session_elapsed + app._turn_elapsed(now)
            else:
                elapsed = session_elapsed
            status = Text("plan" if app.get_plan_state() else "normal")
            status.append(" (Shift+Tab 切换)", style="bright_black")
            status.append("  ·  ", style="bright_black")
            status.append_text(
                present_session_metrics(
                    app.agent_view_store.session_snapshot(),
                    elapsed,
                )
            )
            processing = (
                not app.coordinator.input_active
                and not app.coordinator.inline_widget_active
                and bool(app._activity or app._retry_deadline is not None)
            )
            if processing:
                status.append("  ·  Ctrl+C 中断", style="bright_black")
            elif app.coordinator.input_active and app._has_sub_agents():
                status.append("  ·  ↓查看 agent", style="bright_black")
            if app._quit_deadline is not None and not processing:
                label = app._quit_shortcut or "Ctrl+C"
                status.append("  ·  ", style="bright_black")
                status.append(f"再按一次 {label} 退出", style="bright_black")
        pending_count, pending_source = app.coordinator.pending_summary
        if pending_count:
            status.append("  ·  ", style="bright_black")
            status.append(f"等待 {pending_count}：{pending_source}", style="yellow")
        signature = rich_text_signature(status)
        if signature == self._status_signature:
            return
        app._status.update(status)
        self._status_signature = signature

    def render_queued_inputs(self) -> None:
        """渲染输入框上方的排队分组；空队列或查看转录时隐藏，不留占位空行。"""
        app = self._app
        previews = app.pending_inputs.snapshot()
        widget = app._queued_widget
        visible = bool(previews) and not app.transcript.viewing_agent_id
        signature = (tuple(previews), app.screen.size.width)
        if not visible:
            widget.display = False
            self._queued_signature = None
            return
        widget.display = True
        if signature == self._queued_signature:
            return
        self._queued_signature = signature
        widget.update(
            present_queued_inputs(previews, width=max(1, app.screen.size.width)),
            layout=False,
        )

    def render_input_status(self) -> None:
        """渲染输入框上方的右对齐状态行。

        段内容在会话内基本静态，按组装结果缓存，仅在变化时重绘。
        后续新增状态段时往 segments 追加即可；无内容时保留空行占位，
        避免输入区随状态有无而上下跳动。
        """
        app = self._app
        segments: list[str] = []
        try:
            info = app.get_model_info()
        except Exception:
            info = None
        if info is not None:
            model, effort = info
            segments.append(f"{model} {effort}")
        line = "  ·  ".join(segments)
        if line == self._input_status_cache:
            return
        self._input_status_cache = line
        app._input_status.update(Text(line), layout=False)
