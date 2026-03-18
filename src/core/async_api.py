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

async def parse_stream_response(stream, stream_output=True):
    """异步流式响应解析（占位符）"""
    raise NotImplementedError("待实现")

async def parse_nonstream_response(response, stream_output=True):
    """异步非流式响应解析（占位符）"""
    raise NotImplementedError("待实现")