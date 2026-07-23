"""Agent 长度截断恢复的回归测试。"""

from __future__ import annotations

import asyncio

from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext
from src.llm.base import LLMResponse
from src.llm.errors import LLMErrorKind


class IdentityNormalizer:
    """不提供消息协议兜底的测试 normalizer。"""

    reasoning_effort = "max"
    _EFFORT_DOWNGRADE = {"max": "high", "high": "medium", "medium": "low"}

    def normalize_messages(self, messages: list[dict]) -> list[dict]:
        """原样复制消息列表。

        Args:
            messages: 待归一化的消息列表。

        Returns:
            仅复制容器、不修改消息内容的新列表。
        """
        return list(messages)

    def next_lower_effort(self, current: str) -> str | None:
        """返回比当前档位更低的推理力度档位。

        Args:
            current: 当前推理力度档位。

        Returns:
            下一更低档位；已到最低档位时为 None。
        """
        return self._EFFORT_DOWNGRADE.get(current)


def _agent_with_identity_normalizer() -> Agent:
    """构造只测试长度恢复 handler 的最小 Agent。

    Returns:
        llm 使用 IdentityNormalizer 的未初始化 Agent 实例。
    """
    agent = object.__new__(Agent)
    agent.llm = IdentityNormalizer()
    return agent


def _truncated_tool_response(content: str = "先准备文件") -> LLMResponse:
    """构造包含半截工具参数的长度截断响应。

    Args:
        content: 工具调用前已生成的可见文本。

    Returns:
        finish_reason 为 length 且携带不完整工具调用的响应。
    """
    tool_calls = {
        0: {
            "id": "call_half",
            "name": "write_file",
            "arguments": '{"path":"large.txt","content":"unfinished',
        }
    }
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason="length",
        assistant_message={
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": "call_half",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"large.txt","content":"unfinished',
                    },
                }
            ],
            "reasoning_content": "包含不应进入历史的推理",
            "_response_output": [{"type": "function_call", "arguments": "unfinished"}],
        },
    )


def test_truncated_tool_call_is_discarded_before_retry() -> None:
    """工具调用截断时只保留文本，并要求重新生成较小的完整调用。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "写一个大文件"}],
        response=_truncated_tool_response(),
    )

    process_state = asyncio.run(agent._on_process_response(ctx))
    retry_state = asyncio.run(agent._on_length_retry(ctx))

    assert process_state is AgentState.LENGTH_RETRY
    assert retry_state is AgentState.LLM_CALL
    assert ctx.length_recoveries == 1
    assert ctx.messages[1] == {"role": "assistant", "content": "先准备文件"}
    assert ctx.messages[2]["role"] == "user"
    assert "未执行" in ctx.messages[2]["content"]
    assert "完整" in ctx.messages[2]["content"]
    assert "拆分" in ctx.messages[2]["content"]
    assert "分块" in ctx.messages[2]["content"]
    assert all("tool_calls" not in message for message in ctx.messages)
    assert all("reasoning_content" not in message for message in ctx.messages)
    assert all("_response_output" not in message for message in ctx.messages)


def test_text_truncation_keeps_existing_continuation_flow() -> None:
    """普通文本截断仍保存 provider 消息并从中断处续写。"""
    agent = _agent_with_identity_normalizer()
    assistant_message = {
        "role": "assistant",
        "content": "未写完的半句",
        "reasoning_content": "provider 需要保留的推理",
    }
    ctx = RunContext(
        messages=[],
        response=LLMResponse(
            content="未写完的半句",
            finish_reason="length",
            assistant_message=assistant_message,
        ),
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_CALL
    assert ctx.messages[0] is assistant_message
    assert "从中断处直接继续" in ctx.messages[1]["content"]


def test_length_recovery_accumulates_all_text_segments() -> None:
    """长度恢复成功时保留首段、中段与终段的完整正文。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(messages=[{"role": "user", "content": "继续写完"}])

    for text in ("前半句", "中间"):
        ctx.response = LLMResponse(
            content=text,
            finish_reason="length",
            assistant_message={"role": "assistant", "content": text},
        )
        assert asyncio.run(agent._on_process_response(ctx)) is AgentState.LENGTH_RETRY
        assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL

    ctx.response = LLMResponse(
        content="后半句",
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": "后半句"},
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    assert ctx.final_text == "前半句中间后半句"


def test_normal_round_replaces_recovered_tool_preamble_even_when_empty() -> None:
    """恢复后工具轮的普通终态以空正文清除旧恢复正文。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "查询后回答"}],
        response=LLMResponse(
            content="旧恢复正文",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "旧恢复正文"},
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.LENGTH_RETRY
    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="工具前言",
        tool_calls={
            0: {"id": "call-1", "name": "lookup", "arguments": "{}"},
        },
        finish_reason="tool_calls",
        assistant_message={
            "role": "assistant",
            "content": "工具前言",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.EXECUTE_TOOLS

    ctx.response = LLMResponse(
        content="",
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": ""},
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    assert ctx.final_text == ""


def test_empty_truncation_discards_response_and_lowers_effort() -> None:
    """完全为空的截断归为 UNKNOWN，丢弃整条响应并降低推理力度重生成。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "开始任务"}],
        response=LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": ""},
        ),
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_CALL
    assert ctx.length_recoveries == 1
    # 丢弃重生成：不向历史追加任何合成 assistant 或续写 user。
    assert ctx.messages == [{"role": "user", "content": "开始任务"}]
    # max 有更低档位 high，降档而非压缩指令。
    assert ctx.length_effort_override == "high"
    assert ctx.length_ephemeral_instruction is None


def test_truncated_tool_call_at_recovery_limit_leaves_valid_history() -> None:
    """达到恢复上限时把半截工具调用收口为结构化输出限制错误。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "写一个大文件"}],
        response=_truncated_tool_response(content=""),
        length_recoveries=3,
        max_length_recoveries=3,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state.value == "llm_failure"
    assert ctx.llm_error is not None
    assert ctx.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert ctx.llm_error.retryable is False
    assert "恢复上限" in ctx.llm_error.message
    assert ctx.messages == [{"role": "user", "content": "写一个大文件"}]


def test_nonempty_text_at_recovery_limit_keeps_prior_history_and_current_user() -> None:
    """终态非空截断正文不进入历史，既有合法工具轮与本轮 user 保持原样。"""
    agent = _agent_with_identity_normalizer()
    prior_history = [
        {"role": "user", "content": "读取版本"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_version",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"VERSION"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_version", "content": "1.0"},
        {"role": "assistant", "content": "当前版本是 1.0。"},
    ]
    current_user = {"role": "user", "content": "生成发布说明"}
    messages = [*prior_history, current_user]
    ctx = RunContext(
        messages=messages,
        round_start_idx=len(prior_history),
        response=LLMResponse(
            content="仍未完成的终态正文",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "仍未完成的终态正文"},
        ),
        length_recoveries=2,
        max_length_recoveries=2,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_FAILURE
    assert ctx.llm_error is not None
    assert ctx.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert ctx.messages == [*prior_history, current_user]


def test_recovery_limit_removes_all_previous_continuation_scaffolding() -> None:
    """恢复耗尽时清除此前截断 assistant 与合成 user，只保留本轮原始 user。"""
    agent = _agent_with_identity_normalizer()
    prior_history = [
        {"role": "user", "content": "先前任务"},
        {"role": "assistant", "content": "先前任务已完成。"},
    ]
    current_user = {"role": "user", "content": "输出长篇报告"}
    messages = [*prior_history, current_user]
    ctx = RunContext(
        messages=messages,
        round_start_idx=len(prior_history),
        response=LLMResponse(
            content="第一段截断正文",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "第一段截断正文"},
        ),
        max_length_recoveries=1,
    )

    first_state = asyncio.run(agent._on_length_retry(ctx))

    assert first_state is AgentState.LLM_CALL
    assert ctx.messages[-2] == {"role": "assistant", "content": "第一段截断正文"}
    assert ctx.messages[-1]["role"] == "user"
    assert "从中断处直接继续" in ctx.messages[-1]["content"]

    ctx.response = LLMResponse(
        content="第二段仍然截断",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "第二段仍然截断"},
    )
    terminal_state = asyncio.run(agent._on_length_retry(ctx))

    assert terminal_state is AgentState.LLM_FAILURE
    assert ctx.llm_error is not None
    assert ctx.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert ctx.messages == [*prior_history, current_user]


def test_recovery_limit_uses_checkpoint_after_compact_rewrites_history() -> None:
    """compact 重写消息后仍按恢复段 checkpoint 清除失败残片。

    Returns:
        None。
    """
    agent = _agent_with_identity_normalizer()
    compacted_messages = [
        {"role": "user", "content": "此前对话的压缩摘要"},
        {"role": "assistant", "content": "已恢复压缩上下文。"},
        {"role": "user", "content": "继续生成长篇报告"},
    ]
    ctx = RunContext(
        messages=compacted_messages,
        round_start_idx=100,
        response=LLMResponse(
            content="压缩后第一段截断正文",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "压缩后第一段截断正文"},
        ),
        max_length_recoveries=1,
    )

    first_state = asyncio.run(agent._on_length_retry(ctx))

    assert first_state is AgentState.LLM_CALL
    assert ctx.messages[-2]["content"] == "压缩后第一段截断正文"
    ctx.response = LLMResponse(
        content="压缩后第二段仍截断",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "压缩后第二段仍截断"},
    )

    terminal_state = asyncio.run(agent._on_length_retry(ctx))

    assert terminal_state is AgentState.LLM_FAILURE
    assert ctx.messages == compacted_messages[:3]


def test_thinking_truncation_discards_all_reasoning_carriers() -> None:
    """思考截断丢弃整条响应，历史不含任何推理载体或续写脚手架。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "深度分析"}],
        response=LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={
                "role": "assistant",
                "content": "",
                "reasoning_content": "半截推理",
                "_anthropic_content": [{"type": "thinking", "thinking": "半截"}],
                "_response_output": [{"type": "reasoning", "summary": []}],
            },
        ),
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_CALL
    assert ctx.length_recoveries == 1
    assert ctx.messages == [{"role": "user", "content": "深度分析"}]
    assert ctx.length_effort_override == "high"
    assert ctx.length_ephemeral_instruction is None
    assert all("reasoning_content" not in message for message in ctx.messages)
    assert all("_anthropic_content" not in message for message in ctx.messages)
    assert all("_response_output" not in message for message in ctx.messages)


def test_thinking_effort_steps_down_then_resets_on_clean_terminal() -> None:
    """连续思考截断逐档降低推理力度，触底转压缩指令，干净终态复位。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "长思考任务"}],
        max_length_recoveries=5,
    )

    def _thinking_length() -> LLMResponse:
        return LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={
                "role": "assistant",
                "content": "",
                "reasoning_content": "推理",
            },
        )

    for expected in ("high", "medium", "low"):
        ctx.response = _thinking_length()
        assert asyncio.run(agent._on_process_response(ctx)) is AgentState.LENGTH_RETRY
        assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
        assert ctx.length_effort_override == expected
        assert ctx.length_ephemeral_instruction is None

    # low 已是最低档，无更低档位 → 一次性压缩指令兜底，override 保持 low。
    ctx.response = _thinking_length()
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.LENGTH_RETRY
    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    assert ctx.length_effort_override == "low"
    assert ctx.length_ephemeral_instruction is not None

    # 历史全程未被追加合成 assistant 或续写 user。
    assert ctx.messages == [{"role": "user", "content": "长思考任务"}]

    # 干净终态复位降档与压缩瞬态。
    ctx.response = LLMResponse(
        content="完成",
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": "完成"},
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    assert ctx.length_effort_override is None
    assert ctx.length_ephemeral_instruction is None


def test_thinking_bottom_of_ladder_uses_compress_instruction_not_history() -> None:
    """无降档阶梯（如 Moonshot）时思考截断设置压缩指令且不写入历史。"""

    class NoLadderNormalizer(IdentityNormalizer):
        """无降档阶梯的测试 normalizer。"""

        _EFFORT_DOWNGRADE: dict[str, str] = {}

    agent = object.__new__(Agent)
    agent.llm = NoLadderNormalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "长思考"}],
        response=LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={
                "role": "assistant",
                "content": "",
                "reasoning_content": "推理",
            },
        ),
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_CALL
    assert ctx.length_effort_override is None
    assert ctx.length_ephemeral_instruction is not None
    assert "压缩" in ctx.length_ephemeral_instruction
    assert ctx.messages == [{"role": "user", "content": "长思考"}]


def test_thinking_truncation_at_recovery_limit_uses_thinking_message() -> None:
    """思考截断达到恢复上限时以思考专属文案收口且不改动历史。"""
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "长思考任务"}],
        response=LLMResponse(
            content="",
            finish_reason="length",
            assistant_message={
                "role": "assistant",
                "content": "",
                "reasoning_content": "推理",
            },
        ),
        length_recoveries=3,
        max_length_recoveries=3,
    )

    state = asyncio.run(agent._on_length_retry(ctx))

    assert state is AgentState.LLM_FAILURE
    assert ctx.llm_error is not None
    assert ctx.llm_error.kind is LLMErrorKind.OUTPUT_LIMIT
    assert ctx.llm_error.retryable is False
    assert "思考阶段" in ctx.llm_error.message
    assert ctx.messages == [{"role": "user", "content": "长思考任务"}]


def test_successful_terminal_resets_length_recovery_checkpoint() -> None:
    """一次恢复成功后，后续长度失败不得回滚已完成的上一段响应。

    Returns:
        None。
    """
    agent = _agent_with_identity_normalizer()
    ctx = RunContext(
        messages=[{"role": "user", "content": "分两步处理"}],
        response=LLMResponse(
            content="第一段截断",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "第一段截断"},
        ),
        max_length_recoveries=1,
    )

    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_CALL
    ctx.response = LLMResponse(
        content="第一段已完成",
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": "第一段已完成"},
    )
    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    completed_history = list(ctx.messages)

    ctx.response = LLMResponse(
        content="新一段再次截断",
        finish_reason="length",
        assistant_message={"role": "assistant", "content": "新一段再次截断"},
    )
    assert asyncio.run(agent._on_length_retry(ctx)) is AgentState.LLM_FAILURE

    assert ctx.messages == completed_history
