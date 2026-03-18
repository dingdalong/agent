import asyncio
import json
from typing import Dict, List, Any, Optional, Tuple, Callable, Union
from openai import APIConnectionError, RateLimitError, APIError
from config import async_client, MODEL_NAME, request_semaphore

async def call_model(
    messages: List[Dict[str, Any]],
    stream: bool = False,
    temperature: float = 1.0,
    tools: Optional[List[Dict]] = None,
    max_retries: int = 3,
    timeout: float = 30.0
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    纯异步模型调用，带指数退避重试和并发控制
    """
    async with request_semaphore:
        for attempt in range(max_retries):
            try:
                async with asyncio.timeout(timeout):
                    response = await async_client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=messages,
                        tools=tools,
                        stream=stream,
                        temperature=temperature,
                        tool_choice="auto" if tools else None
                    )

                    if stream:
                        return await parse_stream_response(response, stream_output=True)
                    else:
                        return await parse_nonstream_response(response, stream_output=False)

            except (APIConnectionError, RateLimitError, asyncio.TimeoutError) as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = 2 ** attempt
                print(f"API错误 ({type(e).__name__})，{wait_time}秒后重试...")
                await asyncio.sleep(wait_time)

            except APIError as e:
                raise

async def parse_stream_response(
    stream,
    stream_output: Union[bool, Callable] = True
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    异步迭代流式响应，支持异步回调
    """
    tool_calls = {}
    content_parts = []
    finish_reason = None

    async for chunk in stream:
        delta = chunk.choices[0].delta

        # 处理文本内容
        if delta.content:
            if not (delta.tool_calls and delta.content.isspace()):
                content_parts.append(delta.content)
                if stream_output:
                    if callable(stream_output):
                        if asyncio.iscoroutinefunction(stream_output):
                            await stream_output(delta.content)
                        else:
                            await asyncio.to_thread(stream_output, delta.content)
                    else:
                        print(delta.content, end="", flush=True)

        # 处理工具调用
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

async def parse_nonstream_response(
    response,
    stream_output: Union[bool, Callable] = True
) -> Tuple[str, Dict[int, Dict[str, str]], Optional[str]]:
    """
    异步解析非流式响应
    """
    message = response.choices[0].message
    content = message.content or ""
    finish_reason = response.choices[0].finish_reason

    # 转换tool_calls为字典格式
    tool_calls_dict = {}
    if message.tool_calls:
        for idx, tool_call in enumerate(message.tool_calls):
            tool_calls_dict[idx] = {
                "id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments
            }

    # 输出处理
    if stream_output:
        if callable(stream_output):
            if asyncio.iscoroutinefunction(stream_output):
                await stream_output(content)
            else:
                await asyncio.to_thread(stream_output, content)
        else:
            print(content)

    return content, tool_calls_dict, finish_reason


async def execute_tool_calls(
    content: str,
    tool_calls: Dict[int, Dict[str, str]],
    tool_executor: Dict[str, Callable]
) -> List[Dict[str, Any]]:
    """
    异步执行工具调用，支持同步/异步混合工具
    """
    if not tool_calls:
        return []

    new_messages = []

    # 构造assistant消息
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

    # 并行执行所有工具调用
    tool_tasks = []
    for idx, tc in tool_calls.items():
        task = asyncio.create_task(_execute_single_tool(tc, tool_executor))
        tool_tasks.append((idx, task))

    # 等待所有工具完成
    results = []
    for idx, task in tool_tasks:
        try:
            result = await task
            results.append((idx, result))
        except Exception as e:
            results.append((idx, f"工具执行异常: {e}"))

    # 按原始顺序构造tool消息
    for idx, result in sorted(results, key=lambda x: x[0]):
        new_messages.append({
            "role": "tool",
            "tool_call_id": tool_calls[idx]["id"],
            "content": str(result)
        })

    return new_messages


async def _execute_single_tool(
    tool_call: Dict[str, str],
    tool_executor: Dict[str, Callable]
) -> Any:
    """执行单个工具调用"""
    try:
        args = json.loads(tool_call["arguments"])
    except json.JSONDecodeError as e:
        return f"参数解析失败: {e}"

    func = tool_executor.get(tool_call["name"])
    if not func:
        return f"未找到工具: {tool_call['name']}"

    try:
        # 自动检测并执行异步/同步函数
        if asyncio.iscoroutinefunction(func):
            return await func(**args)
        else:
            return await asyncio.to_thread(func, **args)
    except Exception as e:
        return f"执行错误: {e}"