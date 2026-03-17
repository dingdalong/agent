import time
from openai import APIConnectionError, RateLimitError
from config import client, MODEL_NAME
from typing import Dict, List, Any, Optional, Tuple, Callable
from .performance import time_function

@time_function()
def call_model_with_retry(
    messages,
    stream=False,
    temperature=1.0,
    tools=None,
    max_retries=3
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    带指数退避重试的模型调用
    """
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools,
                stream=stream, # 是否流式返回
                temperature = temperature, # 生成温度
                tool_choice="auto" if tools else None
            )
            if stream == True:
                return parse_stream_response(response, stream_output=True)
            else:
                return parse_nonstream_response(response, stream_output=False)
        except (APIConnectionError, RateLimitError) as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"API 错误，{wait}秒后重试...")
            time.sleep(wait)

import json
from typing import Dict, List, Any, Optional, Tuple, Callable

def parse_nonstream_response(
    response,
    stream_output: bool = True
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    解析非流式响应，提取文本内容和工具调用信息。
    若 stream_output 为 True，则打印内容（或调用回调函数）。

    参数:
        response: OpenAI 风格的完整响应对象（如 ChatCompletion），包含 choices[0].message
        stream_output: 若为 True 则打印 content；若为可调用对象，则将其作为回调函数传入 content
    返回:
        (content, tool_calls_dict, finish_reason)
    """
    message = response.choices[0].message
    content = message.content or ""  # 保证为字符串
    finish_reason = response.choices[0].finish_reason

    # 将 tool_calls 列表转换为与流式函数一致的字典格式
    tool_calls_dict = {}
    if message.tool_calls:
        for idx, tool_call in enumerate(message.tool_calls):
            tool_calls_dict[idx] = {
                "id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments  # 已经是 JSON 字符串
            }

    # 输出处理
    if stream_output:
        if callable(stream_output):
            stream_output(content)
        else:
            print(content)

    return content, tool_calls_dict, finish_reason

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
