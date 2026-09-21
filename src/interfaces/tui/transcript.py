"""Agent 转录视图的目标、渲染 worker 与滚动位置。

视图状态集中在这里，App 只创建应用、在 compose 中挂载控件并把事件回调委托进来，
使历史区渲染与转录渲染彼此独立。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.containers import Vertical
from textual.widgets import Markdown

from src.interfaces.status_presenter import present_agent_identity, present_ended_agent
from src.interfaces.tui.widgets import SelectionStatic, TranscriptPanel

if TYPE_CHECKING:
    from src.interfaces.tui.app import AgentTuiApp

_TRANSCRIPT_MIN_RENDER_INTERVAL = 0.5  # 秒：两次 transcript 渲染之间的最低间隔


@dataclass(slots=True)
class TranscriptPosition:
    """单个 Agent 转录的滚动位置与是否跟随底部。"""

    scroll_y: float = 0.0
    follow: bool = True


@dataclass(slots=True)
class TranscriptRenderRequest:
    """一次待渲染的转录请求；合并队列只保留最新一条。"""

    uuid: str
    version: int
    force: bool
    restore_focus: bool
    invoked: bool
    signature: tuple[Any, ...]
    completed: asyncio.Future[bool] | None = None


class TranscriptController:
    """转录覆盖层的目标切换、增量渲染与滚动位置。

    控件在这里创建、由 App 在 compose 中挂载：初次打开、实时刷新和左右切换都进入同一个
    常驻 worker，worker 串行完成当前 `Markdown.update()`，并只保留一个最新待处理目标；
    旧版本完成后不得恢复焦点、滚动位置或标题。
    """

    def __init__(self, app: AgentTuiApp) -> None:
        self.app = app
        self.zone = Vertical(id="transcript-zone")
        self.header = SelectionStatic("", id="transcript-header", markup=False)
        self.panel = TranscriptPanel(id="transcript-panel")
        self.content = Markdown("", id="transcript-content")
        self.viewing_agent_id: str | None = None
        self.ids: list[str] = []
        self.positions: dict[str, TranscriptPosition] = {}
        self.pending: TranscriptRenderRequest | None = None
        self.merged_requests = 0
        self.active_renders = 0
        self.rendered_id: str | None = None
        self.worker_task: asyncio.Task[None] | None = None
        self._index = 0
        self._signature: tuple[Any, ...] | None = None
        self._requested_signature: tuple[Any, ...] | None = None
        self._generation = 0
        self._incremental_uuid: str | None = None
        self._rendered_source_len = 0
        self._last_render_time = 0.0
        self._render_event = asyncio.Event()

    def start(self) -> None:
        """启动常驻渲染 worker（on_mount 调用一次）。"""
        self.worker_task = asyncio.create_task(self._worker(), name="tui-transcript-render")

    async def stop(self) -> None:
        """取消 worker 并等待其收尾（on_unmount / shutdown 调用）。"""
        task = self.worker_task
        self.worker_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def reset(self) -> None:
        """丢弃所有 Agent 的滚动位置（会话切换时调用）。"""
        self.positions.clear()

    async def open(
        self,
        uuid: str,
        source_ids: list[str],
        *,
        invoked: bool,
    ) -> bool:
        """打开或切换到某个 Agent 的转录；目标不存在时返回 False。"""
        if self.app.agent_view_store.agent_snapshot(uuid) is None:
            return False
        self._set_target(uuid, source_ids, invoked=invoked)
        await self._request_render(
            force=True,
            restore_focus=True,
            invoked=invoked,
        )
        return True

    def _set_target(
        self,
        uuid: str,
        source_ids: list[str],
        *,
        invoked: bool,
    ) -> None:
        """切换显示目标并保存上一个目标的滚动位置。"""
        if invoked:
            self.app.set_focus_target("composer")
        self.save_position()
        ids = [
            item
            for item in source_ids
            if self.app.agent_view_store.agent_snapshot(item) is not None
        ]
        if uuid not in ids:
            ids.append(uuid)
        self.viewing_agent_id = uuid
        self.ids = ids
        self._index = ids.index(uuid)
        self.app.set_screen_class(True, "viewing")
        self.zone.display = True
        self.app.input_area.sync_state()
        self.app.refresh_chrome()

    def save_position(self) -> None:
        """记录当前目标的滚动位置；渲染期间不采样，避免写入过渡几何。"""
        if self.active_renders:
            return
        uuid = self.rendered_id
        if uuid is None:
            return
        position = self.positions.setdefault(
            uuid,
            TranscriptPosition(),
        )
        position.scroll_y = self.panel.scroll_y
        position.follow = self.panel.scroll_y >= self.panel.max_scroll_y - 1

    def schedule_refresh(self) -> None:
        """按内容签名决定是否排队一次实时刷新（每次 tick 调用）。"""
        if not self.viewing_agent_id or self.app.coordinator.inline_widget_active:
            return
        self.save_position()
        signature = self._current_signature(self.viewing_agent_id)
        if signature == self._requested_signature:
            self.update_header(invoked=self.app.coordinator.view_request is not None)
            return
        self._queue_render(
            force=False,
            restore_focus=False,
            invoked=self.app.coordinator.view_request is not None,
            signature=signature,
        )

    def _current_signature(self, uuid: str | None = None) -> tuple[Any, ...]:
        """转录内容的稳定签名：目标 UUID 加 Store 的转录签名。"""
        uuid = uuid if uuid is not None else self.viewing_agent_id or ""
        return (uuid, *self.app.agent_view_store.transcript_signature(uuid))

    async def _request_render(
        self,
        *,
        force: bool,
        restore_focus: bool,
        invoked: bool | None = None,
    ) -> bool:
        completed = self._queue_render(
            force=force,
            restore_focus=restore_focus,
            invoked=(
                self.app.coordinator.view_request is not None
                if invoked is None
                else invoked
            ),
        )
        return await completed if completed is not None else False

    def _queue_render(
        self,
        *,
        force: bool,
        restore_focus: bool,
        invoked: bool,
        signature: tuple[Any, ...] | None = None,
    ) -> asyncio.Future[bool] | None:
        """把请求并入待渲染队列：同签名跳过，被顶掉的请求立刻以 False 结束。"""
        uuid = self.viewing_agent_id
        if uuid is None:
            return None
        signature = signature or self._current_signature(uuid)
        if not force and signature == self._requested_signature:
            return None
        self._generation += 1
        version = self._generation
        completed = asyncio.get_running_loop().create_future()
        request = TranscriptRenderRequest(
            uuid=uuid,
            version=version,
            force=force,
            restore_focus=restore_focus,
            invoked=invoked,
            signature=signature,
            completed=completed,
        )
        previous = self.pending
        if previous is not None:
            self.merged_requests += 1
            if previous.completed is not None and not previous.completed.done():
                previous.completed.set_result(False)
        self.pending = request
        self._requested_signature = signature
        self._render_event.set()
        response_active, thinking_active = self.app.active_streams
        self.app.diagnostics.record(
            "transcript_render_queued",
            version=version,
            target_uuid=uuid,
            merged_requests=self.merged_requests,
            response_stream_active=response_active,
            thinking_stream_active=thinking_active,
        )
        return completed

    async def _worker(self) -> None:
        self.app.diagnostics.record(
            "transcript_worker_started",
        )
        try:
            while True:
                await self._render_event.wait()
                self._render_event.clear()
                while self.pending is not None:
                    request = self.pending
                    self.pending = None
                    rendered = False
                    self.active_renders += 1
                    try:
                        rendered = await self.render(request)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.app.log.error("TUI transcript render failed: %s", exc)
                        self.app.diagnostics.record_exception(
                            "transcript_render_failed",
                            exc,
                            version=request.version,
                            target_uuid=request.uuid,
                        )
                    finally:
                        self.active_renders -= 1
                        if request.completed is not None and not request.completed.done():
                            request.completed.set_result(rendered)
                    # 节流：让事件循环有空间处理 keyboard event（ESC）
                    if self.pending is not None:
                        elapsed = time.monotonic() - self._last_render_time
                        remaining = _TRANSCRIPT_MIN_RENDER_INTERVAL - elapsed
                        if remaining > 0:
                            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise
        finally:
            pending = self.pending
            if pending is not None and pending.completed is not None and not pending.completed.done():
                pending.completed.set_result(False)
            self.app.diagnostics.record(
                "transcript_worker_stopped",
            )

    async def render(
        self,
        request: TranscriptRenderRequest,
    ) -> bool:
        started = time.monotonic()
        if request.version != self._generation:
            return False
        if not request.force and request.signature == self._signature:
            return False
        uuid = request.uuid
        position = self.positions.setdefault(uuid, TranscriptPosition())
        new_source = self.source(uuid)
        if request.version != self._generation:
            return False

        # 增量渲染：若同一 agent 且新 source 以上次渲染为前缀，只 append 增量部分
        incremental = False
        prev_len = self._rendered_source_len
        if (
            not request.force
            and self._incremental_uuid == uuid
            and prev_len > 0
            and len(new_source) >= prev_len
            and new_source[:prev_len] == self.content.source[:prev_len]
        ):
            delta = new_source[prev_len:]
            if delta:
                await self.content.append(delta)
            await self.panel.wait_for_refresh()
            incremental = True
        else:
            await self.content.update(new_source)
            self.rendered_id = uuid
            # 流式 agent 减少 wait_for_refresh 轮数（很快会有下一次渲染）
            agent_snapshot = self.app.agent_view_store.agent_snapshot(uuid)
            max_refreshes = 2 if (agent_snapshot and agent_snapshot.running) else 4
            previous_geometry: tuple[int, float] | None = None
            for _ in range(max_refreshes):
                await self.panel.wait_for_refresh()
                geometry = (
                    self.panel.virtual_size.height,
                    self.panel.max_scroll_y,
                )
                if geometry == previous_geometry:
                    break
                previous_geometry = geometry

        self._incremental_uuid = uuid
        self._rendered_source_len = len(new_source)
        self.rendered_id = uuid

        current = (
            self.viewing_agent_id == uuid
            and request.version == self._generation
        )
        if current:
            if position.follow:
                self.panel.scroll_end(animate=False, force=True, immediate=True)
            else:
                self.panel.scroll_to(
                    y=position.scroll_y,
                    animate=False,
                    force=True,
                    immediate=True,
                )
            position.scroll_y = self.panel.scroll_y
            self._signature = request.signature
            self.update_header(invoked=request.invoked)
            if request.restore_focus:
                self.panel.focus()
            self.app.chrome.mark_dirty()

        self._last_render_time = time.monotonic()
        self.app.diagnostics.record(
            "transcript_render_finished",
            version=request.version,
            target_uuid=uuid,
            current=current,
            incremental=incremental,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            merged_requests=self.merged_requests,
        )
        return current

    def source(self, uuid: str) -> str:
        messages = self.app.agent_view_store.transcript_messages(uuid)
        blocks: list[str] = []
        if messages:
            for message in messages:
                blocks.extend(self._message_blocks(message))
            for kind, text in self.app.agent_view_store.transcript_segments(uuid):
                if kind in {"retry", "error"}:
                    blocks.append(f"### {'⚠ 重试' if kind == 'retry' else '✘ 错误'}\n\n{text}")
            return "\n\n".join(blocks)
        labels = {
            "response": "● 助手",
            "thinking": "◇ 思考",
            "tool": "⚙ 工具",
            "retry": "⚠ 重试",
            "error": "✘ 错误",
        }
        for kind, text in self.app.agent_view_store.transcript_segments(uuid):
            blocks.append(f"### {labels.get(kind, kind)}\n\n{text}")
        return "\n\n".join(blocks)

    def _message_blocks(self, message: dict) -> list[str]:
        if not isinstance(message, dict):
            return [f"### [消息]\n\n{message!s}"]
        role = message.get("role", "")
        blocks: list[str] = []
        if role == "user":
            blocks.append(f"### ▶ 用户\n\n{message.get('content') or ''}")
        elif role == "assistant":
            thinking = message.get("reasoning_content") or message.get("reasoning")
            if thinking:
                blocks.append(f"### ◇ 思考\n\n{thinking}")
            content = message.get("content")
            if content:
                blocks.append(f"### ● 助手\n\n{content}")
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, (list, tuple)):
                tool_calls = [tool_calls]
            for call in tool_calls:
                if not isinstance(call, dict):
                    blocks.append(f"### ⚙ 工具调用\n\n{call!s}")
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    blocks.append(f"### ⚙ 工具调用\n\n{function!s}")
                    continue
                arguments = self._format_tool_arguments(function.get("arguments", ""))
                blocks.append(
                    f"### ⚙ {function.get('name', '')}\n\n````json\n{arguments}\n````"
                )
        elif role == "tool":
            tail = str(message.get("tool_call_id") or "").split("-")[0]
            blocks.append(
                f"### ⚙ 结果 ({tail})\n\n{message.get('content') or ''}"
            )
        else:
            blocks.append(f"### [{role}]\n\n{message.get('content') or ''}")
        return blocks

    @staticmethod
    def _format_tool_arguments(arguments: Any) -> str:
        if not isinstance(arguments, str):
            try:
                return json.dumps(arguments, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                return str(arguments)
        try:
            return json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            return str(arguments)

    def update_header(self, *, invoked: bool) -> None:
        """刷新标题行：身份、跟随状态与按键提示。"""
        if not self.viewing_agent_id:
            return
        snapshot = self.app.agent_view_store.agent_snapshot(self.viewing_agent_id)
        label = (
            present_agent_identity(snapshot, show_status=False).plain
            if snapshot is not None
            else present_ended_agent(self.viewing_agent_id).plain
        )
        position = self.positions.setdefault(
            self.viewing_agent_id,
            TranscriptPosition(),
        )
        distance = max(0, self.panel.max_scroll_y - position.scroll_y)
        follow = "实时" if position.follow else f"已上滚 {distance:.0f} 行"
        exit_hint = "Esc 返回列表" if invoked else "Esc 关闭"
        self.header.update(
            f"── {label} ──  {follow}  ·  ↑/↓ 滚动 · ←/→ 切换 · {exit_hint}"
        )

    def switch(self, delta: int) -> None:
        """在同一批 Agent 内左右切换转录，越界或目标失效时保持或收起。"""
        if self.app.coordinator.inline_widget_active or not self.viewing_agent_id:
            return
        ids = [
            uuid
            for uuid in self.ids
            if self.app.agent_view_store.agent_snapshot(uuid) is not None
        ]
        if self.viewing_agent_id not in ids:
            self.hide()
            return
        self.ids = ids
        self._index = ids.index(self.viewing_agent_id)
        next_index = min(
            max(0, self._index + delta),
            len(self.ids) - 1,
        )
        if next_index == self._index:
            return
        previous_index = self._index
        uuid = self.ids[next_index]
        invoked = self.app.coordinator.view_request is not None
        self._set_target(uuid, self.ids, invoked=invoked)
        self._queue_render(
            force=True,
            restore_focus=True,
            invoked=invoked,
        )
        self.app.diagnostics.record(
            "transcript_switched",
            version=self._generation,
            previous_index=previous_index,
            target_index=next_index,
            target_uuid=uuid,
        )

    def close(self) -> None:
        """关闭转录；内嵌交互持有键盘时忽略，避免抢走按键。"""
        if not self.app.coordinator.inline_widget_active:
            self.app.coordinator.close_transcript()

    def hide(self) -> None:
        """收起转录覆盖层，并让在途渲染与缓存签名失效。"""
        self.save_position()
        self._generation += 1
        pending = self.pending
        self.pending = None
        if pending is not None and pending.completed is not None and not pending.completed.done():
            pending.completed.set_result(False)
        self.viewing_agent_id = None
        self.ids = []
        self._signature = None
        self._requested_signature = None
        self._incremental_uuid = None
        self._rendered_source_len = 0
        self.zone.display = False
        self.app.set_screen_class(False, "viewing")
        self.app.input_area.sync_state()
        self.app.refresh_chrome()
        self.app.diagnostics.record(
            "transcript_hidden",
            version=self._generation,
        )
