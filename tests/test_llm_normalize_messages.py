"""LLM 消息规范化与工具调用配对的回归测试。"""

from __future__ import annotations

import logging

import pytest

from src.llm.base import LLMProvider, LLMResponse
from src.llm.anthropic import AnthropicProvider
from src.llm.deepseek import DeepSeekProvider
from src.llm.moonshot import MoonshotProvider
from src.llm.ollama import OllamaProvider
from src.llm.openai import OpenAIProvider
from src.tools import ToolDict


class StubProvider(LLMProvider):
    """只使用基类消息规范化逻辑的测试 provider。"""

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """返回测试不关心的固定 token 数。

        Args:
            messages: 会话消息。
            prompt: 可选系统提示词。
            tools: 可选工具 schema。

        Returns:
            固定值 0。
        """
        return 0

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        enable_thinking: bool = True,
    ) -> LLMResponse:
        """阻止测试意外发起聊天调用。

        Args:
            messages: 会话消息。
            prompt: 可选系统提示词。
            tools: 可选工具 schema。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            caller_agent_type: 调用方 agent 类型。
            caller_uuid: 调用方实例 UUID。
            enable_thinking: 是否启用思考。

        Returns:
            本测试方法不会返回。
        """
        raise AssertionError("测试不应调用 _do_chat")

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        """模拟 provider 回注原始工具调用载体。

        Args:
            msg: 原始消息。
            norm_msg: 归一化目标消息。
            role: 归一化后的角色。

        Returns:
            None；目标消息会被就地修改。
        """
        if role == "assistant" and msg.get("_provider_call_payload"):
            norm_msg["_provider_call_payload"] = msg["_provider_call_payload"]


def _provider() -> StubProvider:
    """构造不连接网络的测试 provider。

    Returns:
        初始化完成的 StubProvider。
    """
    return StubProvider(api_key="", base_url="", model="stub", event_bus=None)


def _tool_call(call_id: str, name: str = "lookup", arguments: str = "{}") -> dict:
    """构造 OpenAI 兼容工具调用。

    Args:
        call_id: 工具调用 ID。
        name: 工具名称。
        arguments: JSON 参数字符串。

    Returns:
        assistant.tool_calls 中的单个调用字典。
    """
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@pytest.mark.parametrize("call_ids", [("call_1",), ("call_1", "call_2")])
def test_complete_tool_round_trip_is_preserved(call_ids: tuple[str, ...]) -> None:
    """完整单工具和多工具往返在规范化后保持原结构。

    Args:
        call_ids: 同一 assistant 消息中的工具调用 ID。
    """
    messages = [
        {
            "role": "assistant",
            "content": "查询中",
            "tool_calls": [_tool_call(call_id) for call_id in call_ids],
        },
        *[
            {"role": "tool", "tool_call_id": call_id, "content": f"result-{call_id}"}
            for call_id in call_ids
        ],
        {"role": "assistant", "content": "查询完成"},
    ]

    assert _provider().normalize_messages(messages) == messages


def test_partially_missing_tool_responses_are_removed_without_payload_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """部分缺失响应时降级为纯文本 assistant，并避免在警告中泄漏参数。

    Args:
        caplog: pytest 日志捕获 fixture。
    """
    secret_arguments = '{"token":"do-not-log"}'
    messages = [
        {
            "role": "assistant",
            "content": "查询中",
            "tool_calls": [
                _tool_call("call_1", arguments=secret_arguments),
                _tool_call("call_2", arguments=secret_arguments),
            ],
            "_provider_call_payload": {"arguments": secret_arguments},
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "partial result"},
        {"role": "user", "content": "继续"},
    ]

    with caplog.at_level(logging.WARNING, logger="src.llm.base"):
        normalized = _provider().normalize_messages(messages)

    assert normalized == [
        {"role": "assistant", "content": "查询中"},
        {"role": "user", "content": "继续"},
    ]
    assert "do-not-log" not in caplog.text
    assert "工具" in caplog.text


def test_empty_invalid_tool_assistant_and_associated_tools_are_deleted() -> None:
    """无可见文本的非法工具组会连同关联 tool 消息一起删除。"""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tool_call("call_1"), _tool_call("call_2")],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "partial"},
        {"role": "user", "content": "next"},
    ]

    assert _provider().normalize_messages(messages) == [
        {"role": "user", "content": "next"}
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
        [
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [_tool_call("call_1")],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "first"},
            {"role": "tool", "tool_call_id": "call_1", "content": "duplicate"},
        ],
    ],
)
def test_orphaned_or_duplicate_tool_messages_are_deleted(messages: list[dict]) -> None:
    """游离或重复响应不会保留在规范化后的历史中。

    Args:
        messages: 包含游离或重复 tool 消息的非法序列。
    """
    assert all(
        message["role"] != "tool"
        for message in _provider().normalize_messages(messages)
    )


@pytest.mark.parametrize(
    "messages",
    [
        [
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [_tool_call("")],
            }
        ],
        [
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [_tool_call("same"), _tool_call("same")],
            },
            {"role": "tool", "tool_call_id": "same", "content": "result"},
        ],
        [
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [_tool_call("missing")],
            }
        ],
        [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
    ],
)
def test_strict_mode_rejects_invalid_tool_sequences(messages: list[dict]) -> None:
    """严格模式对空 ID、重复 ID、缺失响应和游离响应抛错。

    Args:
        messages: 待校验的非法消息序列。
    """
    with pytest.raises(ValueError, match="工具|tool"):
        _provider().normalize_messages(messages, strict=True)


@pytest.mark.parametrize(
    ("provider_type", "provider_extra"),
    [
        (
            OpenAIProvider,
            {"_response_output": [{"type": "reasoning", "id": "reasoning_1"}]},
        ),
        (
            AnthropicProvider,
            {
                "_anthropic_content": [
                    {
                        "type": "thinking",
                        "thinking": "reasoning",
                        "signature": "signature",
                    }
                ]
            },
        ),
        (DeepSeekProvider, {"reasoning_content": "reasoning"}),
        (OllamaProvider, {"reasoning_content": "reasoning"}),
        (MoonshotProvider, {"reasoning_content": "reasoning"}),
    ],
    ids=["openai", "anthropic", "deepseek", "ollama", "moonshot"],
)
def test_provider_only_assistant_carrier_is_preserved(
    provider_type: type[LLMProvider],
    provider_extra: dict[str, object],
) -> None:
    """只有 provider 专属字段的 assistant 载体在规范化后仍保留。

    Args:
        provider_type: 待验证的真实 provider 类型。
        provider_extra: 该 provider 需要跨轮保留的专属字段。

    Returns:
        None。
    """
    provider = object.__new__(provider_type)
    message = {"role": "assistant", "content": "", **provider_extra}

    assert provider.normalize_messages([message]) == [message]


@pytest.mark.parametrize(
    "provider_type",
    [
        OpenAIProvider,
        AnthropicProvider,
        DeepSeekProvider,
        OllamaProvider,
        MoonshotProvider,
    ],
    ids=["openai", "anthropic", "deepseek", "ollama", "moonshot"],
)
def test_provider_drops_truly_empty_assistant(
    provider_type: type[LLMProvider],
) -> None:
    """没有正文、工具调用或专属字段的 assistant 在规范化时删除。

    Args:
        provider_type: 待验证的真实 provider 类型。

    Returns:
        None。
    """
    provider = object.__new__(provider_type)

    assert provider.normalize_messages(
        [{"role": "assistant", "content": ""}]
    ) == []


def test_moonshot_preserves_reasoning_content_for_valid_tool_round_trip() -> None:
    """Moonshot 的合法工具往返继续携带必需的 reasoning_content。"""
    provider = object.__new__(MoonshotProvider)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "reasoning",
            "tool_calls": [_tool_call("call_1")],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    normalized = provider.normalize_messages(messages)

    assert normalized[0]["reasoning_content"] == "reasoning"
    assert normalized[0]["tool_calls"] == messages[0]["tool_calls"]
    assert normalized[1] == messages[1]
