"""OutputRouter — 消费端事件路由器：主 agent 实时渲染，子 agent 输出按 agent 缓存不交叉。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from src.events.types import (
    CompactDelta,
    Event,
    LLMCallCompleted,
    OutputRequested,
    PermissionNotice,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.events.menu import (
    ChoiceMenu,
    FormMenu,
    InputMenu,
    PermissionMenu,
)
from src.interfaces.base import UserInterface

# 每个 agent 转录的最大分段数（deque maxlen，append/逐出均为 O(1)；连续同类流文本合并进一段）
_MAX_TRANSCRIPT_SEGMENTS = 400
# 已完成 agent 视图最多保留数（超出按 ended_monotonic 逐出最旧项）
_MAX_COMPLETED_VIEWS = 20


@dataclass
class _AgentView:
    """单个 agent 的运行时状态与缓冲转录。

    主 agent 转录留空（其输出已实时在滚动区）；子 agent 转录由后台事件增量填充。
    """

    agent_type: str
    is_main: bool
    running: bool = True
    in_tokens: int = 0
    out_tokens: int = 0
    cache_read: int = 0
    started_monotonic: float | None = None
    ended_monotonic: float | None = None
    # 分段转录：每项为 (kind, text)，kind ∈ "response"|"thinking"|"tool"；供 UI 按类型分别渲染。
    transcript: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TRANSCRIPT_SEGMENTS))


@dataclass
class AgentRow:
    """供 UI 列表渲染的 agent 行快照（每帧从 agent_rows() 拉取）。"""

    uuid: str
    agent_type: str
    is_main: bool
    running: bool
    in_tokens: int
    out_tokens: int
    cache_read: int
    started_monotonic: float | None
    ended_monotonic: float | None


class OutputRouter:
    """消费端事件路由器。

    在 _consume_events 与 ui.on_event 之间插入：
    - 主（前台）agent 事件实时转发到 UI 渲染
    - 子 agent 的流/工具事件按 agent 进缓冲转录，不交叉
    - 控制面事件（全部菜单事件 + 权限通知/输出请求）始终实时转发
    - SubagentLifecycle 自消费，维护 _agents 视图
    - 非 TTY 透传模式：全部实时转发、不缓存、无列表
    """

    def __init__(self, ui: UserInterface, passthrough: bool = False) -> None:
        """初始化路由器。

        Args:
            ui: 目标 UserInterface 实例。
            passthrough: True 时全部事件实时转发（非 TTY 模式）。
        """
        self.ui = ui
        self.passthrough = passthrough
        self._foreground_uuid: str | None = None
        self._agents: dict[str, _AgentView] = {}

    # ---- 前台管理 ----

    def set_foreground(self, uuid: str, agent_type: str = "agent") -> None:
        """登记根 agent uuid，建主 _AgentView(is_main=True)。

        在 _reset_session 创建 agent 后调用，仅主 agent 有此身份。

        Args:
            uuid: 根 agent 的 uuid 字符串。
            agent_type: agent 类型标识。
        """
        self._foreground_uuid = uuid
        self._agents[uuid] = _AgentView(agent_type=agent_type, is_main=True)

    # ---- 事件分发 ----

    async def dispatch(self, event: Event) -> None:
        """按决策表分发事件。

        - 透传模式：全部实时转发（SubagentLifecycle 除外，直接丢弃）
        - 控制面（全部菜单事件 + 权限通知/输出请求）：始终实时
        - LLMCallCompleted：先按 agent 累计 token，再转发（保持 UI 会话累计）
        - SubagentLifecycle：自消费，维护 _agents 视图
        - CompactDelta：始终实时
        - 流/工具事件：前台实时，后台进缓冲

        Args:
            event: 待分发的事件。
        """
        if self.passthrough:
            if not isinstance(event, SubagentLifecycle):
                await self.ui.on_event(event)
            return

        # 控制面事件始终实时（全部菜单事件 + 权限通知 + 输出请求）
        if isinstance(event, (InputMenu, ChoiceMenu, FormMenu, PermissionMenu, PermissionNotice, OutputRequested)):
            await self.ui.on_event(event)
            return

        # LLMCallCompleted：先累计 per-agent token，再转发（保持 UI 会话累计不变）
        if isinstance(event, LLMCallCompleted):
            self._accumulate_tokens(event)
            await self.ui.on_event(event)
            return

        # SubagentLifecycle：自消费，不转发 UI
        if isinstance(event, SubagentLifecycle):
            self._handle_lifecycle(event)
            return

        # CompactDelta：始终实时（仅一行 [compact]，无正文交叉）
        if isinstance(event, CompactDelta):
            await self.ui.on_event(event)
            return

        # 流/工具事件：后台判定
        # LLMCallStarted 落入此处且无匹配缓冲分支 — 后台 spinner 信息无需缓存，静默丢弃。
        if self._is_background(event):
            self._buffer_event(event)
            return

        # 前台：实时转发
        await self.ui.on_event(event)

    def _is_background(self, event: Event) -> bool:
        """事件属于后台当且仅当：携带身份且 caller_uuid != 前台 uuid。

        复用现成 caller_uuid 字段，不改 emit 链路。

        Args:
            event: 待判定的事件。

        Returns:
            True 表示该事件应进缓冲而非实时渲染。
        """
        caller_uuid = getattr(event, "caller_uuid", None)
        if caller_uuid is None:
            return False
        return caller_uuid != self._foreground_uuid

    # ---- token 累计 ----

    def _accumulate_tokens(self, event: LLMCallCompleted) -> None:
        """按 caller_uuid 累计每 agent 的 token 用量。

        Args:
            event: LLM 调用完成事件，含 caller_uuid 与 token 字段。
        """
        caller_uuid = getattr(event, "caller_uuid", None)
        if caller_uuid is None:
            return
        view = self._agents.get(caller_uuid)
        if view is None:
            return
        view.in_tokens += event.input_tokens or 0
        view.out_tokens += event.output_tokens or 0
        view.cache_read += event.cache_read_input_tokens or 0

    # ---- 后台缓冲 ----

    def _buffer_event(self, event: Event) -> None:
        """把后台事件追加到对应 _AgentView 转录（纯内存 append，不 await）。

        SubagentLifecycle start 应先于流事件到达；若缺失则防御性建项。

        Args:
            event: 待缓冲的后台事件。
        """
        caller_uuid: str | None = getattr(event, "caller_uuid", None)
        if caller_uuid is None:
            return
        view = self._agents.get(caller_uuid)
        if view is None:
            # 防御性建项：正常情况下 SubagentLifecycle start 先于流事件到达
            agent_type = getattr(event, "caller_agent_type", None) or "?"
            view = _AgentView(agent_type=agent_type, is_main=False)
            self._agents[caller_uuid] = view

        if isinstance(event, ResponseDelta):
            self._append_transcript(view, "response", event.content)
        elif isinstance(event, ThinkingDelta):
            self._append_transcript(view, "thinking", event.content)
        elif isinstance(event, ToolCallStarted):
            detail = event.detail.strip()
            line = f"● {event.tool_name}"
            if detail:
                line += f" {detail}"
            view.transcript.append(("tool", line + "\n"))
        elif isinstance(event, ToolCallCompleted):
            preview = (event.result_preview or "").strip()
            preview_lines = preview.splitlines()
            first = preview_lines[0] if preview_lines else ("完成" if event.status == "success" else "失败")
            view.transcript.append(("tool", f"  ⎿ {first}  ({event.duration_seconds:.2f}s)\n"))

    def _append_transcript(self, view: _AgentView, kind: str, text: str) -> None:
        """把一段流式文本并入转录：与末段同类则合并进末段，否则新起一段。

        合并保证整块 Markdown 完整（避免跨 delta 把代码围栏/列表拆碎而渲染错乱）。

        Args:
            view: 目标 agent 视图。
            kind: 文本类型（"response" 或 "thinking"）。
            text: 本次增量文本（空则忽略）。
        """
        if not text:
            return
        if view.transcript and view.transcript[-1][0] == kind:
            view.transcript[-1] = (kind, view.transcript[-1][1] + text)
        else:
            view.transcript.append((kind, text))

    # ---- 生命周期 ----

    def _handle_lifecycle(self, event: SubagentLifecycle) -> None:
        """处理 SubagentLifecycle：建/销 agent 视图。

        start: 建项、记起点 monotonic
        end: 置 running=False、记终点 monotonic，保留转录供回看

        Args:
            event: 子 agent 生命周期事件。
        """
        if event.phase == "start":
            view = _AgentView(
                agent_type=event.agent_type,
                is_main=False,
                started_monotonic=time.monotonic(),
            )
            self._agents[event.agent_uuid] = view
        elif event.phase == "end":
            view = self._agents.get(event.agent_uuid)
            if view is not None:
                view.running = False
                view.ended_monotonic = time.monotonic()
            self._prune_completed()

    def _prune_completed(self) -> None:
        """裁剪已完成的 agent 视图，至多保留最近 _MAX_COMPLETED_VIEWS 个。

        运行中视图恒保留；仅按 ended_monotonic 逐出最旧的已完成项。
        """
        completed = [
            (uid, v) for uid, v in self._agents.items()
            if not v.is_main and not v.running and v.ended_monotonic is not None
        ]
        if len(completed) <= _MAX_COMPLETED_VIEWS:
            return
        # 按 ended_monotonic 升序，移除最旧的
        completed.sort(key=lambda x: x[1].ended_monotonic)  # type: ignore[arg-type]
        to_remove = completed[:len(completed) - _MAX_COMPLETED_VIEWS]
        for uid, _ in to_remove:
            del self._agents[uid]

    # ---- UI 数据接口 ----

    def agent_rows(self) -> list[AgentRow]:
        """返回 UI 列表渲染的 agent 行快照。

        主 agent 行置顶，其余子 agent 按插入序（dict 保持插入序）。

        Returns:
            AgentRow 列表，供 UI 每帧拉取。
        """
        rows: list[AgentRow] = []
        # 主 agent 置顶
        for uid, view in self._agents.items():
            if view.is_main:
                rows.append(AgentRow(
                    uuid=uid, agent_type=view.agent_type, is_main=True,
                    running=view.running, in_tokens=view.in_tokens,
                    out_tokens=view.out_tokens, cache_read=view.cache_read,
                    started_monotonic=view.started_monotonic,
                    ended_monotonic=view.ended_monotonic,
                ))
                break
        # 其余按插入序
        for uid, view in self._agents.items():
            if not view.is_main:
                rows.append(AgentRow(
                    uuid=uid, agent_type=view.agent_type, is_main=False,
                    running=view.running, in_tokens=view.in_tokens,
                    out_tokens=view.out_tokens, cache_read=view.cache_read,
                    started_monotonic=view.started_monotonic,
                    ended_monotonic=view.ended_monotonic,
                ))
        return rows

    def transcript_segments(self, uuid: str) -> list[tuple[str, str]]:
        """返回指定 agent 的转录分段快照（只产数据，不碰 rich/markdown）。

        Args:
            uuid: 目标 agent 的 uuid 字符串。

        Returns:
            分段列表，每项为 (kind, text)，kind ∈ "response"|"thinking"|"tool"；无该 agent 时为空列表。
        """
        view = self._agents.get(uuid)
        if view is None:
            return []
        return list(view.transcript)

    # ---- 重置 ----

    def reload(self) -> None:
        """/clear 时清空所有 agent 视图。"""
        self._agents.clear()
