from tools import tools, tool_executor
from core.stream import parse_stream_response, execute_tool_calls
from core.api import call_model_with_retry

def main():
    messages = [
        {"role": "system", "content": "你是一个完美的助手。"},
        {"role": "user", "content": "广州天气和 123/0 的结果"}
    ]

    # 第一次调用
    stream = call_model_with_retry(messages, stream=True, tools=tools)
    content, tool_calls, finish_reason = parse_stream_response(stream, stream_output=True)

    if tool_calls:
        new_messages = execute_tool_calls(content, tool_calls, tool_executor)
        messages.extend(new_messages)

        # 第二次调用（将工具结果返回）
        stream = call_model_with_retry(messages, stream=True, tools=tools)
        content, tool_calls, finish_reason = parse_stream_response(stream, stream_output=True)

if __name__ == "__main__":
    main()