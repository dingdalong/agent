import time
from openai import APIConnectionError, RateLimitError
from config import client, MODEL_NAME

def call_model_with_retry(messages, stream, tools=None, max_retries=3):
    """
    带指数退避重试的模型调用
    """
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools,
                stream=stream,
                tool_choice="auto" if tools else None
            )
        except (APIConnectionError, RateLimitError) as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"API 错误，{wait}秒后重试...")
            time.sleep(wait)