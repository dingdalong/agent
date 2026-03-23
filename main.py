import re
import asyncio

from src.tools import tools, tool_executor
from src.core.async_api import call_model, async_input
from src.memory.memory import ConversationBuffer, VectorMemory
from config import USER_ID
from src.core.performance import async_time_function
from tools.tool_call import execute_tool_calls
from core.guardrails import InputGuardrail, OutputGuardrail
input_guard = InputGuardrail()
output_guard = OutputGuardrail()

from src.plan.integration import handle_planning_request

def _build_collection_name(prefix: str, user_id: str | None) -> str:
    if not user_id:
        return prefix

    sanitized_user_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized_user_id:
        return prefix

    return f"{prefix}_{sanitized_user_id}"[:63].strip("_")

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

# 判断是否为复杂请求（通过大模型判断）
async def is_complex_request(text: str) -> bool:
    # /plan 开头强制规划
    if text.startswith("/plan"):
        return True

    messages = [
        {"role": "system", "content": "你是一个请求分类器。判断用户的请求是否是一个需要拆解为多个步骤来执行的复杂任务。只回复 yes 或 no。"},
        {"role": "user", "content": text}
    ]
    content, _, _ = await call_model(messages, stream=False, temperature=0)
    return "yes" in content.lower()

@async_time_function()
async def run_agent(user_input: str, memory: ConversationBuffer, system_prompt: str = None):
    # 护栏：输入检查
    passed, reason = input_guard.check(user_input)
    if not passed:
        print(f"\n[安全拦截] {reason}")
        return "抱歉，您的输入包含不安全内容，已被拦截。"

    if await is_complex_request(user_input):
        # 规划模式
        await handle_planning_request(
            user_input,
            tools,          # 工具描述列表
            tool_executor,  # ToolExecutor 实例
            async_input     # 异步输入函数
        )
        return ""  # 规划模式内部已打印所有输出，返回空

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

    enhanced_system = "你是一个很棒的助手！"
    if memory_sections:
        enhanced_system = enhanced_system + "\n\n" + "\n\n".join(memory_sections)

    # 1. 将用户消息加入短期记忆
    memory.add_user_message(user_input)

    # 2. 多轮工具调用循环
    max_tool_calls = 5
    tool_call_count = 0
    while True:
        tool_call_count += 1
        if tool_call_count > max_tool_calls:
            # 防止无限循环，添加一条助手消息告知用户
            memory.add_assistant_message({"role": "assistant", "content": "抱歉，工具调用次数过多，请稍后重试或简化问题。"})
            break

        enhanced_system = enhanced_system + "\n\n如果工具返回错误，请分析错误信息并尝试重新调用（调整参数），或向用户解释。"
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

    user_facts.add_conversation(user_input)

    if memory.should_compress():
        memory.compress(conversation_summaries)

    passed, reason = output_guard.check(final_response)
    if not passed:
        final_response = "抱歉，生成的回复包含不安全内容，已过滤。"

    return final_response


async def main():
    memory = ConversationBuffer(max_rounds=10)

    print("Agent 已启动，输入 'exit' 退出。")
    while True:
        user_input = await async_input("\n你: ")
        if user_input.lower() in ["exit", "quit"]:
            break

        print("助手: ", end="", flush=True)
        await run_agent(user_input, memory)
        print()  # 确保换行


if __name__ == "__main__":
    asyncio.run(main())
