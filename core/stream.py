import json
from typing import Dict, List, Any, Optional, Tuple, Callable

def parse_stream_response(
    stream,
    stream_output: bool = True
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    解析流式响应，提取文本内容和工具调用信息。
    若 stream_output 为 True，则实时打印内容（或调用回调函数）。
    """
    tool_calls = {}
    content_parts = []
    finish_reason = None

    for chunk in stream:
        delta = chunk.choices[0].delta

        if delta.content:
            if not (delta.tool_calls and delta.content.isspace()):
                content_parts.append(delta.content)
                if stream_output:
                    if callable(stream_output):
                        stream_output(delta.content)
                    else:
                        print(delta.content, end="", flush=True)

        if delta.tool_calls:
            for tool_chunk in delta.tool_calls:
                idx = tool_chunk.index
                if idx not in tool_calls:
                    tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                if tool_chunk.id:
                    tool_calls[idx]["id"] = tool_chunk.id
                if tool_chunk.function.name:
                    tool_calls[idx]["name"] += tool_chunk.function.name
                if tool_chunk.function.arguments:
                    tool_calls[idx]["arguments"] += tool_chunk.function.arguments

        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason

    if stream_output and not callable(stream_output):
        print()

    content = "".join(content_parts)
    return content, tool_calls, finish_reason


def execute_tool_calls(
    content: str,
    tool_calls: Dict[int, Dict[str, str]],
    tool_executor: Dict[str, Callable]
) -> List[Dict[str, Any]]:
    """
    根据工具调用字典执行函数，返回要追加到消息列表的新消息
    （包含 assistant 消息和所有 tool 消息）。
    """
    if not tool_calls:
        return []

    new_messages = []

    assistant_msg = {
        "role": "assistant",
        "content": content if content else None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"]
                }
            }
            for tc in tool_calls.values()
        ]
    }
    new_messages.append(assistant_msg)

    for idx, tc in tool_calls.items():
        try:
            args = json.loads(tc["arguments"])
        except json.JSONDecodeError:
            result = f"工具参数解析失败：{tc['arguments']}"
        else:
            func = tool_executor.get(tc["name"])
            if func:
                try:
                    result = func(**args)
                except Exception as e:
                    result = f"执行工具 {tc['name']} 时出错：{e}"
            else:
                result = f"未找到工具 {tc['name']}"

        new_messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": str(result)
        })

    return new_messages