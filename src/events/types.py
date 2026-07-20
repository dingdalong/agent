"""事件类型定义 — 所有 EventBus 可发布的事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.events.levels import EventLevel


@dataclass
class Event:
    """事件基类。"""

    timestamp: float
    source: str
    level: EventLevel
    type: str = ""


# 交互式菜单事件（PermissionMenu/InputMenu/ChoiceMenu/FormMenu 及基类 MenuRequest、
# 载荷 FormQuestion）见 src/events/menu.py；统一从 src.events 包出口导入。


# --- PROGRESS 级别 ---

@dataclass
class ResponseDelta(Event):
    """流式回应 — 默认可见。"""
    content: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["token_delta"] = field(default="token_delta", init=False)


@dataclass
class ThinkingDelta(Event):
    """思考过程 — 仅 DETAIL 级别投递/渲染（progress 级别下由总线门控丢弃，状态栏「思考中」由 LLMCallStarted 维持）。"""
    content: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.DETAIL, init=False)
    type: Literal["thinking_delta"] = field(default="thinking_delta", init=False)

@dataclass
class CompactDelta(Event):
    content: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["compact_delta"] = field(default="compact_delta", init=False)


@dataclass
class ToolCallStarted(Event):
    """工具调用开始 — 默认可见。"""
    tool_name: str = ""
    tool_call_id: str = ""
    detail: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["tool_call_started"] = field(default="tool_call_started", init=False)


@dataclass
class ToolCallCompleted(Event):
    """工具调用完成 — 默认可见。"""
    tool_name: str = ""
    tool_call_id: str = ""
    status: Literal["success", "error"] = "success"
    duration_seconds: float = 0.0
    result_preview: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["tool_call_completed"] = field(default="tool_call_completed", init=False)


@dataclass
class LLMCallStarted(Event):
    """LLM 调用开始时的 token 估算信息。

    级别为 PROGRESS：内联状态条在 LLM 调用一开始（首个增量到达前）就需要据此点亮 spinner；
    context_limit 携带当前 provider 的上下文窗口上限，供 UI 按 agent 计算占用比例。
    """
    model: str = ""
    context_limit: int = 0
    estimated_input_tokens: int = 0
    message_count: int = 0
    tool_count: int = 0
    caller_agent_type: str | None = None  # 发起本次调用的 agent 类型（主 Agent 为 None），供 UI 活动行显示当前 agent
    caller_uuid: str | None = None  # 发起本次调用的 agent 实例 uuid
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_started"] = field(default="llm_call_started", init=False)


@dataclass
class LLMCallCompleted(Event):
    """LLM 调用完成后的 token usage 与速度信息。

    级别为 PROGRESS：内联状态条要用 token/缓存命中数据实时更新，必须确保该事件不被级别门控丢弃。
    """
    model: str = ""
    # 统一约定：提交给模型的全部输入 token（含缓存读取与写入）。各 provider 在 _extract_token_usage 中归一化到此口径。
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    duration_seconds: float | None = None
    output_tokens_per_second: float | None = None
    total_tokens_per_second: float | None = None
    caller_uuid: str | None = None  # 发起本次调用的 agent 实例 uuid，供 AgentViewStore 按 agent 累计 token
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_completed"] = field(default="llm_call_completed", init=False)


@dataclass
class OutputRequested(Event):
    """请求 UI 串行输出文本。"""
    content: str = ""
    markdown: bool = False  # 是否按 Markdown 渲染（如计划内容、hook 拦截说明等消息型内容）
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["output_requested"] = field(default="output_requested", init=False)


@dataclass
class InterruptRequested(Event):
    """请求中断当前用户交互或 agent 工作。"""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["interrupt_requested"] = field(default="interrupt_requested", init=False)


@dataclass
class PermissionNotice(Event):
    """工具权限状态通知，供 UI 自行组织展示。"""
    status: Literal["allow", "deny", "auto_allow"] = "allow"
    tool_name: str = ""
    detail: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_notice"] = field(default="permission_notice", init=False)


@dataclass
class AgentStateChanged(Event):
    """Agent 状态机状态转换。"""
    agent_id: str = ""
    agent_type: str = ""
    from_state: str = ""
    to_state: str = ""
    level: EventLevel = field(default=EventLevel.DETAIL, init=False)
    type: Literal["agent_state_changed"] = field(default="agent_state_changed", init=False)


@dataclass
class SubagentLifecycle(Event):
    """子 agent 生命周期事件 — 由 subagent_mgr 在启动/结束时发射，供 AgentViewStore 维护 agent 视图。

    Attributes:
        agent_uuid: 子 agent 实例 uuid 字符串。
        agent_type: 子 agent 类型标识。
        phase: "start" 启动 / "end" 结束。
        messages: 仅 phase=="end" 携带 —— 子 agent 结束时的完整原始消息记录（Agent.history 浅拷贝），
            供 /agents 回看；"start" 阶段为 None。
    """
    agent_uuid: str = ""
    agent_type: str = ""
    phase: Literal["start", "end"] = "start"
    messages: list[dict] | None = None
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["subagent_lifecycle"] = field(default="subagent_lifecycle", init=False)


@dataclass
class PermissionModeChanged(Event):
    """权限模式已变更 — 通知 UI 重读明确的 permission-mode provider 并重绘。

    无 payload：UI 以 pull 模型读取最新权限模式，本事件仅作重绘信号。
    级别为 PROGRESS：避免被级别门控丢弃，保证状态栏即时刷新。
    """
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_mode_changed"] = field(default="permission_mode_changed", init=False)
