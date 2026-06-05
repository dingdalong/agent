from enum import Enum
from dataclasses import dataclass, field

from src.llm.base import LLMResponse


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
    EXECUTE_TOOLS = "execute_tools"
    CHECK_STOP = "check_stop"
    POST_ROUND = "post_round"
    SUMMARIZE_EXIT = "summarize_exit"
    CONTEXT_OVERFLOW = "context_overflow"
    DONE = "done"


@dataclass
class RunContext:
    """一次 Agent.run() 的全部可变状态。"""
    messages: list[dict]
    prompt: list[dict] | None = None
    final_text: str = ""
    has_tool_calls: bool = False
    rounds_without_todo: int = 0
    round_start_idx: int = 0
    compact_streak: int = 0
    max_compact_streak: int = 3
    stop_hook_used: bool = False
    length_recoveries: int = 0
    max_length_recoveries: int = 3
    response: LLMResponse | None = None
    manual_compact: bool = False
    compact_focus: str | None = None
    user_input: str = ""
    command: tuple[str, list[str]] | None = None
    exit_requested: bool = False


@dataclass
class RunResult:
    """Agent.run() 的返回值。

    /plan 和 /mode 在 agent 内部处理，不会出现在 command 中。
    仅 /clear 会通过 command 字段传递给 app 层。

    Attributes:
        final_text: LLM 最终输出文本。
        command: 需要 app 层处理的斜杠命令（仅 /clear），无命令时为 None。
        exit_requested: 用户是否请求退出（输入 exit/quit 或输入被取消）。
        user_input: 用户原始输入文本。
    """
    final_text: str = ""
    command: tuple[str, list[str]] | None = None
    exit_requested: bool = False
    user_input: str = ""
