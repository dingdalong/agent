"""在线探针：被中断轮次的历史形状是否被各 provider 接受。

本用例默认不运行（``-m integration`` 显式开启），因为它需要真实 API 凭据，并按
provider 计费。它回答的是离线测试无法回答的问题：把「只有推理载体」「推理载体+
未执行的工具调用」「推理载体+完整正文」「只有正文」这些中断收束形状交回模型时，
provider 是否接受、以及模型能否接着完成任务。

运行方式（未配置的 provider 自动跳过）：

    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=... \
    DEEPSEEK_API_KEY=... DEEPSEEK_MODEL=... \
    OPENAI_API_KEY=... OPENAI_MODEL=... \
    MOONSHOT_API_KEY=... MOONSHOT_MODEL=... \
        uv run pytest tests/test_interrupt_history_probe.py -m integration -s

显式环境变量缺失时，凭据回退到配置层的 dotenv 链（``~/.agent/.env`` →
``{workdir}/.env`` → ``{workdir}/.agent/.env``），模型回退到配置链
``role.default`` 角色槽位（``~/.agent/config.yaml`` → ``{workdir}/.agent/config.yaml``）
中该 provider 的 ``model.default``。因此在已配置好的仓库里可以直接运行：

    uv run pytest tests/test_interrupt_history_probe.py -m integration -s

每个形状都会打印完整请求与响应，便于人工核对；断言只有两条：请求不报错、追加
一条「继续」后模型能给出非空回复。框架现在对完整推理载体一律回放（不再有
provider 能力位），探针因此作为各 provider 接受度的观测记录：DeepSeek
（deepseek-flash，2026-09-22）六个形状全部通过；Anthropic 与 OpenAI 的形状
接受度可按需在线复核。

种子与追问在同一次 ``asyncio.run`` 内完成：provider 的 HTTP 客户端跨事件循环
复用连接池会被 SDK 包装成 ``APIConnectionError``，看起来像 provider 拒绝。
离线用例 ``test_probe_runs_seed_and_follow_up_in_one_event_loop`` 守护这一不变式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - 环境缺 python-dotenv 时降级为只读环境变量
    dotenv_values = None  # type: ignore[assignment]

from src.common.paths import builtin_root, global_data_dir, project_data_dir, workdir
import pytest
import yaml

from src.llm.anthropic import AnthropicProvider
from src.llm.base import LLMProvider, LLMResponse
from src.llm.deepseek import DeepSeekProvider
from src.llm.errors import LLMCallError
from src.llm.moonshot import MoonshotProvider
from src.llm.openai import OpenAIProvider

ECHO_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "probe_echo",
        "description": "回显一段文本，用于探针测试。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}

#: 先要一句正文、再要工具调用：让 message、reasoning、function_call 三类载体
#: 都可能出现在种子响应里，六个形状才都有可测素材。
SEED_PROMPT = (
    "先用一句话说明你的意图，然后调用 probe_echo 工具，参数 text 为 hi。"
    "不要在正文里给出最终答案。"
)

#: 逐个探测的中断收束形状；顺序与设计矩阵一致。
SHAPES = (
    "orphan_reasoning",
    "orphan_reasoning_from_tool_turn",
    "reasoning_with_tool_call",
    "reasoning_with_content",
    "content_only",
    "tool_call_only",
)

_PROVIDER_TYPES: dict[str, Callable[..., LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "moonshot": MoonshotProvider,
}

_DOTENV_LOGGER = logging.getLogger("dotenv.main")


def _builtin_provider_config(name: str) -> dict[str, Any]:
    """读取内置 provider 配置（base_url、超时等）。"""
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())
    return dict(builtin.get("llm_provider", {}).get(name, {}))


def _dotenv_chain() -> tuple[Path, ...]:
    """返回配置层使用的 dotenv 优先级链（低→高）。"""
    root = workdir()
    return (global_data_dir() / ".env", root / ".env", project_data_dir(root) / ".env")


def _setting(key: str) -> str:
    """读取连接配置：显式环境变量优先，其次 dotenv 链。"""
    explicit = os.environ.get(key, "")
    if explicit:
        return explicit
    if dotenv_values is None:
        return ""
    for path in _dotenv_chain():
        try:
            # .env 里可能存在非 dotenv 的自由文本，python-dotenv 会为每一行发出
            # warning；探针只关心能解析出的键，临时静音以免淹没请求/响应输出。
            level = _DOTENV_LOGGER.level
            _DOTENV_LOGGER.setLevel(logging.ERROR)
            try:
                values = dotenv_values(path)
            finally:
                _DOTENV_LOGGER.setLevel(level)
        except (OSError, ValueError):
            continue
        value = values.get(key)
        if value:
            return str(value)
    return ""


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """把 overlay 深度合并进 base，返回新字典。"""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _role_model(name: str) -> str:
    """从配置链的角色槽位推导该 provider 实际使用的模型 ID。"""
    config: dict[str, Any] = {}
    root = workdir()
    for path in (global_data_dir() / "config.yaml", project_data_dir(root) / "config.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            config = _deep_merge(config, data)
    role_section = config.get("role")
    if not isinstance(role_section, dict):
        return ""
    role_name = role_section.get("default") or "coding"
    slots = role_section.get(role_name)
    model_slot = slots.get("model") if isinstance(slots, dict) else None
    reference = model_slot.get("default") if isinstance(model_slot, dict) else None
    if not isinstance(reference, str) or "/" not in reference:
        return ""
    provider_name, model_id = reference.split("/", 1)
    return model_id if provider_name == name else ""


def _provider(name: str) -> LLMProvider:
    """按环境变量构造真实 provider；缺凭据或模型时跳过。"""
    prefix = name.upper()
    api_key = _setting(f"{prefix}_API_KEY")
    model = _setting(f"{prefix}_MODEL") or _role_model(name)
    if not api_key or not model:
        pytest.skip(f"缺少 {prefix}_API_KEY / {prefix}_MODEL，且配置链未提供")
    config = _builtin_provider_config(name)
    return _PROVIDER_TYPES[name](
        api_key=api_key,
        base_url=_setting(f"{prefix}_API_URL") or config.get("base_url", ""),
        model=model,
        event_bus=None,
        # 只对 NETWORK/TIMEOUT/SERVICE/RATE_LIMIT/RESPONSE_PROTOCOL 重试；400 等
        # 非重试错误仍然第一次就抛出，不会掩盖 provider 的真实拒绝。
        max_attempts=3,
    )


def _carrier_family(message: dict[str, Any]) -> str:
    """返回助手消息所属的载体族。"""
    if message.get("_anthropic_content"):
        return "anthropic"
    if message.get("_response_output"):
        return "responses"
    return "chat"


def _split_carrier(message: dict[str, Any]) -> dict[str, Any]:
    """把真实助手消息拆成推理载体、工具调用与正文三部分。

    Args:
        message: provider 归一化后的助手消息。

    Returns:
        含 ``family``、``reasoning``、``tool_call``、``content`` 的字典；三个内容
        分别是对应载体族里的原始块/输出项/字段值，缺失时为 None。
    """
    family = _carrier_family(message)
    if family == "anthropic":
        blocks = message["_anthropic_content"]
        return {
            "family": family,
            "reasoning": next((b for b in blocks if b.get("type") == "thinking"), None),
            "tool_call": next((b for b in blocks if b.get("type") == "tool_use"), None),
            "content": next((b for b in blocks if b.get("type") == "text"), None),
        }
    if family == "responses":
        items = message["_response_output"]
        return {
            "family": family,
            "reasoning": next((i for i in items if i.get("type") == "reasoning"), None),
            "tool_call": next((i for i in items if i.get("type") == "function_call"), None),
            "content": next((i for i in items if i.get("type") == "message"), None),
        }
    tool_calls = message.get("tool_calls") or []
    return {
        "family": family,
        "reasoning": message.get("reasoning_content") or None,
        "tool_call": tool_calls[0] if tool_calls else None,
        "content": message.get("content") or None,
    }


def _assistant_message(family: str, *carriers: Any) -> dict[str, Any]:
    """按载体族把若干原始载体组装成一条助手消息。"""
    items = [carrier for carrier in carriers if carrier]
    if family == "anthropic":
        return {"role": "assistant", "_anthropic_content": list(items)}
    if family == "responses":
        return {"role": "assistant", "_response_output": list(items)}
    message: dict[str, Any] = {"role": "assistant", "content": None}
    for carrier in items:
        if isinstance(carrier, dict) and carrier.get("type") == "function":
            message["tool_calls"] = [carrier]
        elif isinstance(carrier, str):
            message["reasoning_content"] = carrier
        else:
            message["content"] = carrier
    return message


def _tool_result_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    """构造未执行工具调用的模型可见结果。

    Responses 的 ``function_call`` 项同时有 item ``id`` 和协议层的 ``call_id``，
    工具结果必须引用 ``call_id``；Anthropic 的 ``tool_use`` 与 chat 族的
    ``tool_calls`` 只有 ``id``，因此按 ``call_id`` → ``id`` 顺序回退。
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("call_id") or tool_call.get("id") or "",
        "content": json.dumps(
            {"status": "cancelled", "error_code": "not_executed"}, ensure_ascii=False
        )
        + "\n该调用未执行即被用户中止。",
    }


def _shape_messages(
    shape: str,
    parts: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """按形状组装历史；缺少该形状需要的载体时返回 None。"""
    family = parts["family"]
    reasoning = parts["reasoning"]
    content = parts["content"]
    tool_call = parts["tool_call"]
    if shape == "orphan_reasoning":
        return [ _assistant_message(family, reasoning) ] if reasoning else None
    if shape == "orphan_reasoning_from_tool_turn":
        if not reasoning or not tool_call:
            return None
        # 用户场景：种子轮思考完整且伴随工具调用，但历史里只回传思考、丢弃工具调用。
        return [ _assistant_message(family, reasoning) ]
    if shape == "reasoning_with_tool_call":
        if not reasoning or not tool_call:
            return None
        return [
            _assistant_message(family, reasoning, tool_call),
            _tool_result_message(tool_call),
        ]
    if shape == "reasoning_with_content":
        if not reasoning or not content:
            return None
        return [ _assistant_message(family, reasoning, content) ]
    if shape == "content_only":
        return [ _assistant_message(family, content) ] if content else None
    if shape == "tool_call_only":
        if not tool_call:
            return None
        return [
            _assistant_message(family, tool_call),
            _tool_result_message(tool_call),
        ]
    raise AssertionError(f"未知形状：{shape}")


def _cause_chain(exc: BaseException) -> str:
    """返回异常原因链文本，暴露被 SDK 归一化的底层错误。

    Args:
        exc: 探针调用抛出的异常。

    Returns:
        ``Type: msg <- Type: msg`` 形式的原因链。
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _carrier_summary(parts: dict[str, Any]) -> str:
    """汇总种子响应实际携带的载体，便于核对 skip 原因。"""
    present = [
        name for name in ("reasoning", "content", "tool_call") if parts.get(name)
    ]
    return f"family={parts['family']} carriers={present or '无'}"


def _print_exchange(provider: LLMProvider, messages: list[dict[str, Any]], response: Any) -> None:
    """打印一次探针往返的请求与响应，供人工核对。"""
    print(f"\n--- {type(provider).__name__} 请求 ---")
    print(json.dumps(provider.normalize_messages(messages), ensure_ascii=False, indent=2)[:4000])
    print(f"--- {type(provider).__name__} 响应 ---")
    print(f"finish_reason={response.finish_reason}")
    print(f"content={response.content!r}")
    print(f"reasoning={response.assistant_message.get('reasoning_content') if response.assistant_message else None!r}")


async def _chat_or_fail(provider: LLMProvider, label: str, **kwargs: Any) -> Any:
    """执行一次探针调用；失败时输出错误类别与原因链后终止用例。"""
    try:
        return await provider.chat(tools=[ECHO_TOOL], enable_thinking=True, **kwargs)
    except LLMCallError as exc:
        pytest.fail(
            f"{type(provider).__name__} {label} 请求失败：kind={exc.info.kind} "
            f"status={exc.info.status_code} message={exc.info.message}\n"
            f"原因链：{_cause_chain(exc)}",
            pytrace=False,
        )


async def _seed_parts(provider: LLMProvider, name: str, shape: str) -> dict[str, Any]:
    """取种子响应载体；缺该形状所需载体时重试一次，仍缺则跳过。"""
    for attempt in range(2):
        seed = await _chat_or_fail(
            provider,
            f"种子(第{attempt + 1}次)",
            messages=[{"role": "user", "content": SEED_PROMPT}],
        )
        parts = _split_carrier(seed.assistant_message or {})
        print(f"\n--- {type(provider).__name__} 种子响应 ---")
        print(f"finish_reason={seed.finish_reason} content={seed.content!r}")
        print(_carrier_summary(parts))
        if _shape_messages(shape, parts) is not None:
            return parts
    pytest.skip(f"{name} 种子响应连续两次缺少 {shape} 需要的载体（{_carrier_summary(parts)}）")


async def _probe_shape_async(name: str, shape: str) -> None:
    """在同一个事件循环里完成种子与追问两次调用。"""
    provider = _provider(name)
    try:
        parts = await _seed_parts(provider, name, shape)
        messages = _shape_messages(shape, parts)
        assert messages is not None
        messages.append({"role": "user", "content": "继续"})
        follow_up = await _chat_or_fail(provider, shape, messages=messages)
        _print_exchange(provider, messages, follow_up)
        assert follow_up.content.strip() or follow_up.tool_calls, (
            f"{name} 在 {shape} 形状下没有产出可继续的内容"
        )
    finally:
        client = getattr(provider, "_client", None)
        close = getattr(client, "close", None)
        if close is not None:
            await close()


def _probe_shape(name: str, shape: str) -> None:
    """对单个 provider 的单个形状执行一次探针。

    种子与追问必须共用同一个事件循环：provider 的 HTTP 客户端在第一个循环里
    建立连接池，跨 `asyncio.run` 复用会拿到已关闭循环的连接，被 SDK 包装成
    ``APIConnectionError``，看起来像 provider 拒绝。
    """
    asyncio.run(_probe_shape_async(name, shape))


@pytest.mark.parametrize("name", tuple(_PROVIDER_TYPES))
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.integration
def test_interrupted_history_shape_continues(name: str, shape: str) -> None:
    """中断收束形状必须被接受，且模型能接着继续。"""
    _probe_shape(name, shape)


def test_tool_result_references_protocol_call_id() -> None:
    """工具结果按载体族选对调用 ID：Responses 用 call_id，其余用 id。"""
    responses_call = {
        "type": "function_call",
        "id": "b77267dd-8862-4fbd-ae06-81c12f4201a7",
        "call_id": "call_abc123",
        "name": "probe_echo",
        "arguments": '{"text": "hi"}',
    }
    assert _tool_result_message(responses_call)["tool_call_id"] == "call_abc123"

    anthropic_call = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "probe_echo",
        "input": {"text": "hi"},
    }
    assert _tool_result_message(anthropic_call)["tool_call_id"] == "toolu_1"

    chat_call = {
        "id": "call_chat",
        "type": "function",
        "function": {"name": "probe_echo", "arguments": '{"text": "hi"}'},
    }
    assert _tool_result_message(chat_call)["tool_call_id"] == "call_chat"


def test_probe_runs_seed_and_follow_up_in_one_event_loop(monkeypatch) -> None:
    """离线护栏：种子与追问必须在同一个事件循环里发出。"""
    loops: list[Any] = []

    class _StubProvider:
        """记录调用所在事件循环的桩 provider。"""

        def __init__(self) -> None:
            self._client = None

        def normalize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return messages

        async def chat(self, **kwargs: Any):
            loops.append(asyncio.get_running_loop())
            del kwargs
            if len(loops) == 1:
                return _stub_response({
                    "role": "assistant",
                    "_response_output": [
                        {"type": "reasoning", "id": "rs_1", "summary": []},
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "probe_echo",
                            "arguments": '{"text": "hi"}',
                        },
                    ],
                })
            return _stub_response({"role": "assistant", "content": "继续完成"})

    monkeypatch.setattr(
        sys.modules[__name__], "_provider", lambda name: _StubProvider(),
    )

    _probe_shape("deepseek", "reasoning_with_tool_call")

    assert len(loops) == 2
    assert loops[0] is loops[1], "种子与追问落到了不同的事件循环"


def _stub_response(assistant_message: dict[str, Any]) -> Any:
    """构造探针桩响应。"""
    return LLMResponse(
        content=assistant_message.get("content") or "",
        tool_calls=[],
        finish_reason="stop",
        assistant_message=assistant_message,
        token_usage=None,
    )
