import re
import logging
import asyncio

from src.tools import tools, tool_executor
from src.core.async_api import call_model, execute_tool_calls
from src.memory.memory import ConversationBuffer, VectorMemory
from config import USER_ID
from src.core.performance import time_function, async_time_function


def _build_collection_name(prefix: str, user_id: str | None) -> str:
    if not user_id:
        return prefix

    sanitized_user_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized_user_id:
        return prefix

    return f"{prefix}_{sanitized_user_id}"[:63].strip("_")


async def async_input(prompt: str = "") -> str:
    """异步输入函数，使用asyncio.to_thread包装input()"""
    return await asyncio.to_thread(input, prompt)


# 初始化长期记忆（每个用户一个集合，这里简单起见使用固定集合）
user_facts = VectorMemory(collection_name=_build_collection_name("user_facts", USER_ID))
conversation_summaries = VectorMemory(
    collection_name=_build_collection_name("conversation_summaries", USER_ID)
)


def _memory_texts(results) -> list[str]:
    texts = []
    for item in results:
        if isinstance(item, dict):
            fact = item.get("fact")
            if fact:
                texts.append(fact)
        else:
            content = item.get_content()
            if content:
                texts.append(content)
    return texts


@async_time_function()
async def run_agent(user_input: str, memory: ConversationBuffer, system_prompt: str = "你是一个完美的助手。"):
    """异步处理单次用户输入，集成长期记忆检索和存储"""
    memory_sections = []

    facts = _memory_texts(user_facts.search(user_input, n_results=10))
    if facts:
        fact_context = "以下是你知道的关于用户的信息：\n" + "\n".join(facts)
        memory_sections.append(fact_context)

    summaries = _memory_texts(conversation_summaries.search(user_input, n_results=10))
    if summaries:
        summary_context = "以下是与当前对话相关的历史摘要：\n" + "\n".join(summaries)
        memory_sections.append(summary_context)

    enhanced_system = system_prompt
    if memory_sections:
        enhanced_system = system_prompt + "\n\n" + "\n\n".join(memory_sections)

    # 1. 将用户消息加入短期记忆
    memory.add_user_message(user_input)

    # 2. 多轮工具调用循环
    while True:
        messages = [{"role": "system", "content": enhanced_system}] + memory.get_messages_for_api()
        content, tool_calls, _ = await call_model(messages, stream=True, tools=tools)

        if not tool_calls:
            # 没有工具调用，将助手回复存入短期记忆并结束
            memory.add_assistant_message({"role": "assistant", "content": content})
            final_response = content
            break

        # 有工具调用：执行工具，并将 assistant 消息和 tool 结果加入短期记忆
        new_messages = await execute_tool_calls(content, tool_calls, tool_executor)
        memory.add_assistant_message(new_messages[0])
        for tool_msg in new_messages[1:]:
            memory.add_tool_message(tool_msg["tool_call_id"], tool_msg["content"])

    user_facts.add_conversation(user_input, final_response)

    if memory.should_compress():
        memory.compress(conversation_summaries)

    return final_response


async def main():
    memory = ConversationBuffer(max_rounds=10)
    system_prompt = "你是一个完美的助手。"

    print("Agent 已启动，输入 'exit' 退出。")
    while True:
        user_input = await async_input("\n你: ")
        if user_input.lower() in ["exit", "quit"]:
            break

        print("助手: ", end="", flush=True)
        await run_agent(user_input, memory, system_prompt)
        print()  # 确保换行


if __name__ == "__main__":
    asyncio.run(main())
