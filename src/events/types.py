"""事件类型定义 — 所有 EventBus 可发布的事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rich.text import Text

from src.events.levels import EventLevel


@dataclass
class Event:
    """事件基类。

    caller_agent_type / caller_uuid 标识发起该事件的 agent（主 Agent 的 agent_type 为「main」，
    子智能体为各自类型；None 表示无 agent 身份，如用户/应用发起的事件）。作为所有事件的一等属性，
    供 OutputRouter 前台/后台分流与 UI 统一标注「是哪个 agent」使用。
    """

    timestamp: float
    source: str
    level: EventLevel
    type: str = ""
    caller_agent_type: str | None = None
    caller_uuid: str | None = None


def caller_identity(agent: object | None) -> tuple[str | None, str | None]:
    """从 agent 实例提取事件 caller 身份，作为所有 emit 点的唯一取值口径。

    Args:
        agent: Agent 实例；None 或缺失对应字段时该项返回 None。
    Returns:
        (caller_agent_type, caller_uuid)：agent_type 取自 agent.agent_type，
        caller_uuid 取 str(agent.uuid)（uuid 缺失时为 None）。
    """
    if agent is None:
        return None, None
    uuid = getattr(agent, "uuid", None)
    return getattr(agent, "agent_type", None), str(uuid) if uuid is not None else None


# 交互式菜单事件（PermissionMenu/InputMenu/ChoiceMenu/FormMenu 及基类 MenuRequest、
# 载荷 FormQuestion）见 src/events/menu.py；统一从 src.events 包出口导入。


# --- PROGRESS 级别 ---

@dataclass
class ResponseDelta(Event):
    """流式回应 — 默认可见。"""
    content: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["token_delta"] = field(default="token_delta", init=False)


@dataclass
class ThinkingDelta(Event):
    """思考过程 — 仅 DETAIL 级别投递/渲染（progress 级别下由总线门控丢弃）。

    DETAIL 级别下状态栏「思考中」由本事件维持；连接/等待首个增量的窗口由 LLMCallStarted
    维持为「等待响应」。progress 级别下本事件被丢弃，「等待响应」持续到首个回应增量。
    """
    content: str = ""
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
    detail: str = ""                    # 保留，向后兼容
    display: object | None = None       # ToolDisplay，仅 UI 消费
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["tool_call_started"] = field(default="tool_call_started", init=False)


@dataclass
class ToolCallCompleted(Event):
    """工具调用完成 — 默认可见。"""
    tool_name: str = ""
    tool_call_id: str = ""
    status: Literal["success", "error"] = "success"
    duration_seconds: float = 0.0
    result_preview: str = ""            # 保留，向后兼容
    display: object | None = None       # ToolDisplay，仅 UI 消费
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["tool_call_completed"] = field(default="tool_call_completed", init=False)


@dataclass
class LLMCallStarted(Event):
    """LLM 单次尝试开始时的序号与 token 估算信息。

    级别为 PROGRESS：内联状态条在 LLM 调用一开始（首个增量到达前）就需要据此点亮 spinner；
    context_limit 携带当前 provider 的上下文窗口上限，供 UI 按 agent 计算占用比例；attempt 与
    max_attempts 标识本次调用在统一自动重试流程中的位置。
    """
    model: str = ""
    context_limit: int = 0
    estimated_input_tokens: int = 0
    message_count: int = 0
    tool_count: int = 0
    attempt: int = 1
    max_attempts: int = 1
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
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_completed"] = field(default="llm_call_completed", init=False)


@dataclass
class LLMRetrying(Event):
    """LLM 调用失败、进入指数退避等待重试时发射，供状态条实时倒计时展示。

    级别为 PROGRESS：状态条要据此在实时重绘区显示倒计时，必须不被级别门控丢弃。

    Attributes:
        error_kind: 稳定的 LLM 错误类别。
        safe_message: 不含请求体、响应体和凭据的错误摘要。
        partial: 失败尝试是否已收到正文、思考或工具片段。
        tool_fragment_state: 工具片段状态，取 none、partial 或 complete。
        attempt: 已失败的尝试序号（1 基）。
        max_attempts: 允许的最大尝试次数。
        wait_seconds: 本次等待秒数（含随机抖动，为原始浮点值，展示时由 UI 向上取整）。
    """
    error_kind: str = ""
    safe_message: str = ""
    partial: bool = False
    tool_fragment_state: str = "none"
    attempt: int = 0
    max_attempts: int = 0
    wait_seconds: float = 0.0
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_retrying"] = field(default="llm_retrying", init=False)


@dataclass
class LLMLengthRetrying(Event):
    """LLM 响应因输出长度上限被截断、进入自动恢复时发射，供 UI 标记与 Store 转录。

    级别为 PROGRESS：保证不被级别门控在总线处丢弃，从而能进入 AgentViewStore 并参与
    前台/后台分流；UI 标记本身不启动倒计时（区别于 LLMRetrying）。

    Attributes:
        truncation_kind: 截断所处阶段，取 tool_call、content、thinking 或 unknown。
        strategy: 采取的恢复策略，取 continue、regenerate-lower-effort 或 regenerate-compress。
        effort: 本次恢复调用将使用的推理力度档位。
        attempt: 本轮已执行的长度恢复次数（1 基）。
        max_attempts: 本轮允许的最大长度恢复次数。
    """
    truncation_kind: str = ""
    strategy: str = ""
    effort: str = ""
    attempt: int = 0
    max_attempts: int = 0
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_length_retrying"] = field(default="llm_length_retrying", init=False)


@dataclass
class LLMCallFailed(Event):
    """LLM 调用不可继续后的安全终态信息。

    级别为 PROGRESS：前台 UI 必须永久展示终态，后台调用则由 AgentViewStore 记录但不直接输出。
    事件只携带分类器生成的安全摘要、有限 provider 元数据与诊断关联 ID，不包含请求体、响应体、
    凭据或原始异常文本。

    Attributes:
        error_kind: 稳定的 LLM 错误类别。
        safe_message: 不含请求体、响应体和凭据的错误摘要。
        attempts: 本次调用实际执行的尝试次数。
        partial: 终态尝试是否已收到正文、思考或工具片段。
        tool_fragment_state: 工具片段状态，取 none、partial 或 complete。
        status_code: 安全提取的 HTTP 状态码。
        provider_code: 安全提取的 provider 错误码。
        request_id: 安全提取的 provider 请求 ID。
        diagnostic_id: 本地安全诊断关联 ID。
    """
    error_kind: str = ""
    safe_message: str = ""
    attempts: int = 0
    partial: bool = False
    tool_fragment_state: str = "none"
    status_code: int | None = None
    provider_code: str | None = None
    request_id: str | None = None
    diagnostic_id: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["llm_call_failed"] = field(default="llm_call_failed", init=False)


@dataclass
class OutputRequested(Event):
    """请求 UI 串行输出纯文本或 Rich 文本。"""
    content: str | Text = ""
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
    status: Literal["allow", "deny"] = "allow"
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
class PlanStateChanged(Event):
    """Plan 状态已变更，通知 UI 重读 provider 并重绘。

    无 payload：UI 以 pull 模型读取最新权限模式，本事件仅作重绘信号。
    级别为 PROGRESS：避免被级别门控丢弃，保证状态栏即时刷新。
    """
    active: bool = False
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["plan_state_changed"] = field(default="plan_state_changed", init=False)
