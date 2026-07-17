"""OutputRouter — 消费端事件路由器：主 agent 实时渲染，子 agent 输出按 agent 缓存不交叉。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from src.events.types import (
    CompactDelta,
    Event,
    LLMCallCompleted,
    LLMCallStarted,
    OutputRequested,
    PermissionNotice,
    ResponseDelta,
    SubagentLifecycle,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    InputMenu,
    PermissionMenu,
    TranscriptView,
)
from src.interfaces.base import UserInterface

# 每个 agent 转录的最大分段数（deque maxlen，append/逐出均为 O(1)；连续同类流文本合并进一段）
_MAX_TRANSCRIPT_SEGMENTS = 400
# 历史子 agent 视图最多保留数（已从实时展示区移除、供 /agents 回看；超出按 ended_monotonic 逐出最旧项）
_MAX_HISTORY_VIEWS = 50


def _fmt_tok(count: int) -> str:
    """把 token 数格式化为紧凑字符串（≥1000 显示为 k）。

    Args:
        count: token 数。
    Returns:
        紧凑字符串（如 "512"、"1.3k"）。
    """
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


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
    # 分段转录：每项为 (kind, text)，kind ∈ "response"|"thinking"|"tool"；供 UI 运行中实时渲染。
    # 仅运行中有意义——子 agent 结束后 messages（完整原始记录）就位并接管查看，转录随即被清空释放。
    transcript: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TRANSCRIPT_SEGMENTS))
    # 完整原始消息记录：子 agent 结束时由 SubagentLifecycle(phase="end") 携入（Agent.history 快照）；
    # 就位后同时供实时列表查看与 /agents 回看（完整参数/结果，不截断）。运行中/主 agent 为 None。
    messages: list[dict] | None = None


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
    - 前台 LLMCallStarted 落在轮边界：先把本轮已完成子 agent 从 _agents 移入 _history，再转发
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
        # 实时展示集：主 agent + 当前轮尚在运行/刚完成的子 agent；每轮边界冲刷已完成项。
        self._agents: dict[str, _AgentView] = {}
        # 历史集：已从实时展示区移除的已完成子 agent，供 /agents 回看，/clear 时清空。
        self._history: dict[str, _AgentView] = {}

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
        - 前台 LLMCallStarted：落在轮边界，先冲刷本轮已完成子 agent 入 _history，再转发点亮 spinner
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
        if isinstance(event, (InputMenu, ChoiceMenu, ChoiceInputMenu, FormMenu, PermissionMenu, TranscriptView, PermissionNotice, OutputRequested)):
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

        # 前台 LLMCallStarted：处于轮边界（子 agent 在父工具调用内同步跑完），
        # 先把本轮已完成子 agent 从实时展示区移入历史，再转发（UI 据此点亮 spinner）。
        if isinstance(event, LLMCallStarted) and getattr(event, "caller_uuid", None) == self._foreground_uuid:
            self.flush_completed_subagents()
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
        view = self._find_view(caller_uuid)
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
        view = self._find_view(caller_uuid)
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
        end: 置 running=False、记终点 monotonic，存完整原始消息并清空实时预览转录（查看改渲原始消息）；
             本轮结束再由 flush 移入历史。

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
                view.messages = event.messages
                if event.messages:  # 完整原始记录就位后查看改渲它，实时预览转录不再被读取——清空释放冗余内存
                    view.transcript.clear()

    def flush_completed_subagents(self) -> None:
        """把实时展示集中已完成的子 agent 移入历史集（轮边界调用）。

        主 agent 与仍运行中的子 agent 保留在 _agents；已完成子 agent 迁入 _history，
        随后对 _history 按 ended_monotonic 逐出最旧项至 _MAX_HISTORY_VIEWS。
        """
        for uid in list(self._agents.keys()):
            view = self._agents[uid]
            if not view.is_main and not view.running:
                self._history[uid] = view
                del self._agents[uid]
        if len(self._history) <= _MAX_HISTORY_VIEWS:
            return
        # 按 ended_monotonic 升序逐出最旧的（None 视为最旧）
        ordered = sorted(self._history.items(), key=lambda x: x[1].ended_monotonic or 0.0)
        for uid, _ in ordered[:len(self._history) - _MAX_HISTORY_VIEWS]:
            del self._history[uid]

    def _find_view(self, uuid: str) -> _AgentView | None:
        """按 uuid 查找 agent 视图：先查实时集，再查历史集。

        Args:
            uuid: 目标 agent 的 uuid 字符串。

        Returns:
            匹配的 _AgentView，均无则 None。
        """
        return self._agents.get(uuid) or self._history.get(uuid)

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
        view = self._find_view(uuid)
        if view is None:
            return []
        return list(view.transcript)

    def _ordered_subagents(self) -> list[tuple[str, _AgentView]]:
        """返回本会话所有子 agent 视图（排除主 agent），历史（已完成）在前、实时存活在后。

        Returns:
            (uuid, _AgentView) 列表，供文本摘要与选择菜单共用。
        """
        ordered: list[tuple[str, _AgentView]] = list(self._history.items())
        ordered += [(uid, v) for uid, v in self._agents.items() if not v.is_main]
        return ordered

    def _format_view_line(self, uid: str, view: _AgentView) -> str:
        """把单个子 agent 视图格式化为一行摘要（供文本摘要与选择菜单标签共用）。

        格式：`◯ <agent_type>  <uid8>  <运行中|已完成>  ↑<in>(<hit>%) ↓<out> · <elapsed>s`。

        Args:
            uid: 子 agent uuid 字符串。
            view: 对应的 _AgentView。
        Returns:
            单行摘要文本。
        """
        if view.ended_monotonic is not None and view.started_monotonic is not None:
            elapsed = view.ended_monotonic - view.started_monotonic
        elif view.running and view.started_monotonic is not None:
            elapsed = time.monotonic() - view.started_monotonic
        else:
            elapsed = 0.0
        hit_pct = (view.cache_read / view.in_tokens * 100) if view.in_tokens else 0.0
        status = "运行中" if view.running else "已完成"
        uid8 = uid.split("-")[0] if uid else ""
        return (
            f"◯ {view.agent_type}  {uid8}  {status}  "
            f"↑{_fmt_tok(view.in_tokens)}({hit_pct:.0f}%) ↓{_fmt_tok(view.out_tokens)} · {elapsed:.1f}s"
        )

    def format_subagent_summary(self) -> str:
        """生成本会话所有子 agent 的文本摘要列表（供非 TTY 回退一次性输出）。

        合并历史集（已完成，先）与实时集中存活的子 agent（运行中，后），排除主 agent。
        无任何子 agent 时返回提示句。

        Returns:
            多行文本摘要；空会话返回「本会话尚未启动任何子 agent。」。
        """
        ordered = self._ordered_subagents()
        if not ordered:
            return "本会话尚未启动任何子 agent。"
        lines = [f"本会话子 agent（{len(ordered)}）:"]
        lines += [self._format_view_line(uid, view) for uid, view in ordered]
        return "\n".join(lines)

    def subagent_choices(self) -> list[tuple[str, str]]:
        """返回子 agent 选择项列表（供 /agents 交互选择菜单）。

        Returns:
            (uuid, 单行摘要标签) 列表；顺序同 format_subagent_summary（历史在前、实时存活在后）；
            无子 agent 时为空列表。
        """
        return [(uid, self._format_view_line(uid, view)) for uid, view in self._ordered_subagents()]

    def transcript_messages(self, uuid: str) -> list[dict]:
        """返回指定 agent 的完整原始消息记录（供 /agents 回看原始消息）。

        Args:
            uuid: 目标 agent 的 uuid 字符串。
        Returns:
            原始消息 dict 列表（Agent.history 快照）；无该 agent 或未捕获时为空列表。
        """
        view = self._find_view(uuid)
        if view is None or view.messages is None:
            return []
        return list(view.messages)

    # ---- 重置 ----

    def reload(self) -> None:
        """/clear 时清空所有 agent 视图（实时集与历史集）。"""
        self._agents.clear()
        self._history.clear()
