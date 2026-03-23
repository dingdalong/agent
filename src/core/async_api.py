import asyncio
from typing import Dict, List, Any, Optional, Tuple, Callable, Union
from openai import APIConnectionError, RateLimitError, APIError
from config import async_client, MODEL_NAME, request_semaphore
from .performance import async_time_function

@async_time_function()
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

async def async_input(prompt: str = "") -> str:
    """异步输入函数，使用asyncio.to_thread包装input()"""
    return await asyncio.to_thread(input, prompt)
