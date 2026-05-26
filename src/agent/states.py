from enum import Enum
from dataclasses import dataclass, field

from src.llm.base import LLMResponse


class AgentState(Enum):
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
