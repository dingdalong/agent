"""性能测试 - 基于 ChatFlow + FSMRunner 的端到端性能基准。

运行方式: pytest -m slow tests/performance_test.py -v -s
"""
import time
import logging
import pytest

from src.flows.chat import ChatFlow
from src.core.fsm import FSMRunner
from src.memory import ConversationBuffer, MemoryStore
from src.tools import tools, tool_executor

logger = logging.getLogger(__name__)

# 所有性能测试默认跳过，运行方式: pytest -m slow
pytestmark = [pytest.mark.slow, pytest.mark.asyncio]


async def _run_chat(user_input: str, memory: ConversationBuffer,
                    store: MemoryStore) -> str:
    """构建 ChatFlow 并执行一轮对话，返回结果。"""
    chat_flow = ChatFlow(
        memory=memory,
        store=store,
        tools_schema=tools,
        tool_executor=tool_executor,
    )
    chat_flow.model.data["user_input"] = user_input
    runner = FSMRunner(chat_flow)
    return await runner.run()


def _make_store(collection_prefix: str = "perf_test") -> MemoryStore:
    """创建测试用的 MemoryStore 实例。"""
    return MemoryStore(collection_name=f"{collection_prefix}_memories")


async def test_single_conversation():
    """测试单轮对话的性能"""
    memory = ConversationBuffer()
    store = _make_store("perf_single")

    start = time.perf_counter()
    response = await _run_chat("你好，我叫大龙，我喜欢喝咖啡", memory, store)
    elapsed = time.perf_counter() - start

    logger.info(f"单轮对话耗时: {elapsed:.2f}s")
    assert response is not None


async def test_multiple_conversations():
    """测试多轮对话的性能"""
    memory = ConversationBuffer()
    store = _make_store("perf_multi")

    test_inputs = [
        "你好，我叫大龙",
        "我喜欢喝咖啡，特别是拿铁",
        "我住在北京",
        "我的工作是软件工程师",
        "我有一只猫叫小花",
    ]

    total_time = 0
    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        await _run_chat(user_input, memory, store)
        elapsed = time.perf_counter() - start
        total_time += elapsed
        logger.info(f"第{i+1}轮: {elapsed:.2f}s")
        print(f"第{i+1}轮: {elapsed:.2f}s")

    avg_time = total_time / len(test_inputs)
    logger.info(f"总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    print(f"总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")


async def test_empty_memory():
    """场景A：空记忆库测试 - 测试基础API调用和事实提取开销"""
    logger.info("=== 场景A：空记忆库测试 ===")
    print("=== 场景A：空记忆库测试 ===")

    memory = ConversationBuffer()
    store = _make_store("perf_empty")

    test_inputs = [
        "你好，我叫小明",
        "我喜欢吃披萨",
        "我今年25岁",
    ]

    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        await _run_chat(user_input, memory, store)
        elapsed = time.perf_counter() - start
        logger.info(f"空记忆库第{i+1}轮: {elapsed:.2f}s")
        print(f"空记忆库第{i+1}轮: {elapsed:.2f}s")


async def test_with_existing_memory():
    """场景B：有记忆库测试 - 测试记忆检索和版本控制开销"""
    logger.info("=== 场景B：有记忆库测试 ===")
    print("=== 场景B：有记忆库测试 ===")

    memory = ConversationBuffer()
    store = _make_store("perf_existing")

    # 先建立一些记忆
    setup_inputs = [
        "你好，我叫大龙",
        "我喜欢喝咖啡",
        "我住在上海",
        "我是程序员",
    ]
    for user_input in setup_inputs:
        await _run_chat(user_input, memory, store)

    # 测试有记忆时的性能
    test_inputs = [
        "我刚才说我叫什么名字？",
        "我喜欢喝什么？",
        "我住在哪里？",
        "我的职业是什么？",
    ]

    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        await _run_chat(user_input, memory, store)
        elapsed = time.perf_counter() - start
        logger.info(f"有记忆库第{i+1}轮: {elapsed:.2f}s")
        print(f"有记忆库第{i+1}轮: {elapsed:.2f}s")


async def test_long_conversation():
    """场景C：长对话测试 - 测试对话压缩和摘要生成开销"""
    logger.info("=== 场景C：长对话测试 ===")
    print("=== 场景C：长对话测试 ===")

    memory = ConversationBuffer(max_rounds=3)
    store = _make_store("perf_long")

    long_inputs = [
        "今天天气不错",
        "我想去公园散步",
        "公园里有好多花",
        "我看到了一只小鸟",
        "小鸟在树上唱歌",
        "我觉得很放松",
        "准备回家了",
        "今天真是美好的一天",
    ]

    total_time = 0
    for i, user_input in enumerate(long_inputs):
        start = time.perf_counter()
        await _run_chat(user_input, memory, store)
        elapsed = time.perf_counter() - start
        total_time += elapsed
        logger.info(f"长对话第{i+1}轮: {elapsed:.2f}s")
        print(f"长对话第{i+1}轮: {elapsed:.2f}s")

    avg_time = total_time / len(long_inputs)
    logger.info(f"长对话总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    print(f"长对话总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")


async def main():
    print("开始性能测试...")
    logger.info("性能测试开始")

    try:
        print("\n1. 单轮对话测试")
        await test_single_conversation()

        print("\n2. 多轮对话测试")
        await test_multiple_conversations()

        print("\n3. 场景A：空记忆库测试")
        await test_empty_memory()

        print("\n4. 场景B：有记忆库测试")
        await test_with_existing_memory()

        print("\n5. 场景C：长对话测试")
        await test_long_conversation()

        print("\n性能测试完成！")
        logger.info("性能测试完成")

    except Exception as e:
        logger.error(f"性能测试失败: {e}", exc_info=True)
        print(f"性能测试失败: {e}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
