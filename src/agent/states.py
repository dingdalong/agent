from __future__ import annotations

from enum import Enum
from dataclasses import dataclass

from src.llm.base import LLMResponse
from src.llm.errors import LLMErrorInfo


def parse_command(user_input: str) -> tuple[str, list[str]] | None:
    """尝试将用户输入解析为斜杠命令。

    按空格分割输入，第一个 token 必须以 "/" 开头才视为命令。
    命令名称转换为小写，参数保留原始大小写。

    Args:
        user_input: 用户原始输入字符串。

    Returns:
        解析成功返回 (命令名称, 参数列表) 元组，命令名称为小写且不含 "/" 前缀；
        输入不是斜杠命令时返回 None。
    """
    stripped = user_input.strip()
    if not stripped or not stripped.startswith("/"):
        return None
    parts = stripped.split()
    name = parts[0][1:].lower()
    args = parts[1:]
    return (name, args)


class AgentState(Enum):
    REQUEST_INPUT = "request_input"
    CHECK_COMPACT = "check_compact"
    COMPACT = "compact"
    LLM_CALL = "llm_call"
    PROCESS_RESPONSE = "process_response"
    LENGTH_RETRY = "length_retry"
    PAUSE_TURN = "pause_turn"
    EXECUTE_TOOLS = "execute_tools"
    CHECK_STOP = "check_stop"
    POST_ROUND = "post_round"
    SUMMARIZE_EXIT = "summarize_exit"
    CONTEXT_OVERFLOW = "context_overflow"
    LLM_FAILURE = "llm_failure"
    DONE = "done"


@dataclass
class RunContext:
    """一次 Agent.run() 的全部可变状态。

    Attributes:
        messages: 当前会话消息列表。
        turn_start_messages: 本轮追加用户消息前的历史浅快照；没有快照时为 None。
        prompt: 当前完整系统提示词。
        final_text: 本轮最终输出文本。
        has_tool_calls: 本轮是否执行过工具调用。
        round_start_idx: 本轮消息在历史中的起始下标。
        compact_streak: 连续有效但仍不足的自动 compact 次数。
        max_compact_streak: 自动 compact 有效但仍不足的保护上限。
        auto_compact_before_tokens: 最近一次自动 compact 前的输入 token 估算；
            无待评估的自动 compact 时为 None。
        auto_compact_summarized_message_count: 最近一次自动 compact 返回的摘要消息数；
            尚未返回自动 compact 结果时为 None。
        auto_compact_has_summary: 最近一次自动 compact 是否返回非空摘要；
            尚未返回自动 compact 结果时为 None。
        stop_hook_used: 本轮 Stop hook 是否已阻止过一次停止。
        length_recoveries: 本轮长度截断恢复次数。
        max_length_recoveries: 本轮长度截断恢复上限。
        response_recovery_start_idx: 当前响应恢复链在消息列表中的起始位置；
            没有待回滚的恢复段时为 None。
        response_recovery_response_count: 当前响应恢复链已收到的 length 或
            pause_turn 成功响应数。
        pause_turn_message_idx: 当前连续 pause_turn 载体在消息列表中的位置；
            没有可替换载体时为 None。
        pause_turn_continuations: 当前响应恢复链已执行的 pause_turn 续接次数。
        length_effort_override: 当前恢复链临时降档后的推理力度；无降档时为 None。
        length_ephemeral_instruction: 触底/无阶梯时下发的一次性压缩推理指令；
            仅作用于下一次调用、永不写回历史，无兜底时为 None。
        response: 最近一次 LLM 响应。
        manual_compact: 当前工具轮是否请求手动 compact。
        compact_focus: 手动 compact 的可选关注点。
        user_input: 本轮用户原始输入。
        command: app 层斜杠命令（由 CommandMgr defer 挂上，主循环二次 dispatch）。
        exit_requested: 用户是否请求退出。
        llm_error: 本轮终态 LLM 错误的安全结构化信息。
    """
    messages: list[dict]
    turn_start_messages: list[dict] | None = None
    prompt: list[dict] | None = None
    final_text: str = ""
    has_tool_calls: bool = False
    round_start_idx: int = 0
    compact_streak: int = 0
    max_compact_streak: int = 3
    auto_compact_before_tokens: int | None = None
    auto_compact_summarized_message_count: int | None = None
    auto_compact_has_summary: bool | None = None
    stop_hook_used: bool = False
    length_recoveries: int = 0
    max_length_recoveries: int = 3
    response_recovery_start_idx: int | None = None
    response_recovery_response_count: int = 0
    pause_turn_message_idx: int | None = None
    pause_turn_continuations: int = 0
    length_effort_override: str | None = None
    length_ephemeral_instruction: str | None = None
    response: LLMResponse | None = None
    manual_compact: bool = False
    compact_focus: str | None = None
    user_input: str = ""
    user_record_id: str | None = None
    command: tuple[str, list[str]] | None = None
    exit_requested: bool = False
    llm_error: LLMErrorInfo | None = None


@dataclass
class RunResult:
    """Agent.run() 的返回值。

    agent 层命令（plan/models/resume/help）在 agent 内由 CommandMgr 处理；
    app 层命令（clear/agents）由 CommandMgr defer 后经 command 字段上抛给 app 主循环二次 dispatch。

    Attributes:
        final_text: LLM 最终输出文本。
        command: 需要 app 层处理的斜杠命令（由 CommandMgr defer 上抛），无命令时为 None。
        exit_requested: 用户是否请求退出（输入 exit/quit 或输入被取消）。
        user_input: 用户原始输入文本。
        llm_error: 本轮终态 LLM 错误的安全结构化信息。
    """
    final_text: str = ""
    command: tuple[str, list[str]] | None = None
    exit_requested: bool = False
    user_input: str = ""
    llm_error: LLMErrorInfo | None = None
