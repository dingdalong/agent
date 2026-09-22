"""assistant 消息的按需具体化视图。

provider 归一化后的 assistant 消息只保留单一权威载体：

- Responses API：``_response_output``（完整输出项）
- Anthropic：``_anthropic_content``（完整 content blocks）
- 其他 provider（Moonshot/Ollama 等）：直接保留 ``content``/``tool_calls``

``content``/``tool_calls``/``reasoning_content`` 是载体的派生物，不再与载体同时落库；
需要它们的调用方（消息序列校验、展示、压缩序列化）在这里按需重建，因此同一段文本
在上下文里只存在一份。
"""

from __future__ import annotations

import json
from typing import Any


def has_carrier(message: object) -> bool:
    """判断消息是否携带 provider 权威载体。"""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    output = message.get("_response_output")
    if isinstance(output, list) and output:
        return True
    content = message.get("_anthropic_content")
    return isinstance(content, list) and bool(content)


def materialize_assistant(message: object) -> Any:
    """返回 assistant 消息的具体化视图。

    Args:
        message: 任意消息；非 assistant 或无载体时原样返回。

    Returns:
        无载体时返回原对象；有载体时返回新建 dict，含从载体重建的
        ``content``/``tool_calls``/``reasoning_content``，并保留其他非派生字段。
    """
    if not has_carrier(message):
        return message
    assert isinstance(message, dict)
    output = message.get("_response_output")
    if isinstance(output, list) and output:
        content, tool_calls, thinking = _from_response_output(output)
    else:
        content, tool_calls, thinking = _from_anthropic_content(message["_anthropic_content"])
    view: dict[str, Any] = {"role": "assistant", "content": content or None}
    for key, value in message.items():
        if key in {"role", "content", "tool_calls", "reasoning_content", "reasoning",
                   "_response_output", "_anthropic_content"}:
            continue
        view[key] = value
    if tool_calls:
        view["tool_calls"] = tool_calls
    if thinking:
        view["reasoning_content"] = thinking
    return view


def materialize_messages(messages: list[dict]) -> list[dict]:
    """返回用于序列化的消息列表：载体消息替换为具体化视图。

    Args:
        messages: 会话消息列表。

    Returns:
        新列表；无载体消息沿用原对象。
    """
    return [
        materialize_assistant(message) if has_carrier(message) else message
        for message in messages
    ]


def _from_response_output(
    items: list[Any],
) -> tuple[str, list[dict[str, Any]], str]:
    """从 Responses 输出项重建正文、工具调用与推理文本。"""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    content_parts.append(block["text"])
        elif item_type == "reasoning":
            thinking_parts.extend(_reasoning_item_text(item))
        elif item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or ""
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            })
    return "".join(content_parts), tool_calls, "".join(thinking_parts)


def _reasoning_item_text(item: dict[str, Any]) -> list[str]:
    """提取单个 Responses reasoning 项的文本片段。"""
    parts: list[str] = []
    for key in ("summary", "content"):
        blocks = item.get(key)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return parts


def _from_anthropic_content(
    blocks: list[Any],
) -> tuple[str, list[dict[str, Any]], str]:
    """从 Anthropic content blocks 重建正文、工具调用与推理文本。"""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                content_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                thinking_parts.append(thinking)
        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(
                        block.get("input", {}), ensure_ascii=False,
                    ),
                },
            })
    return "".join(content_parts), tool_calls, "".join(thinking_parts)
