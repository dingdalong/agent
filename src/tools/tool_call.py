"""批量异步执行 LLM 返回的 tool_calls。"""

import asyncio
import json
from typing import Any, Dict, List

from .router import ToolRouter


async def execute_tool_calls(
    content: str,
    tool_calls: Dict[int, Dict[str, str]],
    router: ToolRouter,
) -> List[Dict[str, Any]]:
    """并行执行工具调用，通过 router 分发。"""
    if not tool_calls:
        return []

    new_messages: list[dict] = []

    # 构造 assistant 消息
    assistant_msg = {
        "role": "assistant",
        "content": content if content else None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            }
            for tc in tool_calls.values()
        ],
    }
    new_messages.append(assistant_msg)

    # 并行执行所有工具调用
    tool_tasks: list[tuple[int, asyncio.Task]] = []
    results: list[tuple[int, str]] = []

    for idx, tc in tool_calls.items():
        try:
            args = json.loads(tc["arguments"])
        except json.JSONDecodeError as e:
            results.append((idx, f"参数 JSON 解析失败: {e}"))
            continue
        task = asyncio.create_task(router.route(tc["name"], args))
        tool_tasks.append((idx, task))

    for idx, task in tool_tasks:
        try:
            result = await task
            results.append((idx, result))
        except Exception as e:
            results.append((idx, f"工具执行异常: {e}"))

    # 按原始顺序构造 tool 消息
    for idx, result in sorted(results, key=lambda x: x[0]):
        new_messages.append({
            "role": "tool",
            "tool_call_id": tool_calls[idx]["id"],
            "content": str(result),
        })

    return new_messages
