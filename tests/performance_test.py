import time
import logging
import pytest
from main import run_agent, ConversationBuffer

logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_single_conversation():
    """测试单轮对话的性能"""
    memory = ConversationBuffer()
    start = time.perf_counter()
    response = await run_agent("你好，我叫大龙，我喜欢喝咖啡", memory)
    elapsed = time.perf_counter() - start
    logger.info(f"单轮对话耗时: {elapsed:.2f}s")
    return elapsed

@pytest.mark.asyncio
async def test_multiple_conversations():
    """测试多轮对话的性能"""
    memory = ConversationBuffer()
    total_time = 0

    test_inputs = [
        "你好，我叫大龙",
        "我喜欢喝咖啡，特别是拿铁",
        "我住在北京",
        "我的工作是软件工程师",
        "我有一只猫叫小花"
    ]

    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        response = await run_agent(user_input, memory)
        elapsed = time.perf_counter() - start
        total_time += elapsed
        logger.info(f"第{i+1}轮: {elapsed:.2f}s")
        print(f"第{i+1}轮: {elapsed:.2f}s")

    avg_time = total_time / len(test_inputs)
    logger.info(f"总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    print(f"总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    return total_time

@pytest.mark.asyncio
async def test_empty_memory():
    """场景A：空记忆库测试 - 测试基础API调用和事实提取开销"""
    logger.info("=== 场景A：空记忆库测试 ===")
    print("=== 场景A：空记忆库测试 ===")
    memory = ConversationBuffer()

    test_inputs = [
        "你好，我叫小明",
        "我喜欢吃披萨",
        "我今年25岁"
    ]

    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        response = await run_agent(user_input, memory)
        elapsed = time.perf_counter() - start
        logger.info(f"空记忆库第{i+1}轮: {elapsed:.2f}s")
        print(f"空记忆库第{i+1}轮: {elapsed:.2f}s")

@pytest.mark.asyncio
async def test_with_existing_memory():
    """场景B：有记忆库测试 - 测试记忆检索和版本控制开销"""
    logger.info("=== 场景B：有记忆库测试 ===")
    print("=== 场景B：有记忆库测试 ===")

    # 先建立一些记忆
    memory = ConversationBuffer()
    setup_inputs = [
        "你好，我叫大龙",
        "我喜欢喝咖啡",
        "我住在上海",
        "我是程序员"
    ]

    for user_input in setup_inputs:
        await run_agent(user_input, memory)

    # 测试有记忆时的性能
    test_inputs = [
        "我刚才说我叫什么名字？",
        "我喜欢喝什么？",
        "我住在哪里？",
        "我的职业是什么？"
    ]

    for i, user_input in enumerate(test_inputs):
        start = time.perf_counter()
        response = await run_agent(user_input, memory)
        elapsed = time.perf_counter() - start
        logger.info(f"有记忆库第{i+1}轮: {elapsed:.2f}s")
        print(f"有记忆库第{i+1}轮: {elapsed:.2f}s")

@pytest.mark.asyncio
async def test_long_conversation():
    """场景C：长对话测试 - 测试对话压缩和摘要生成开销"""
    logger.info("=== 场景C：长对话测试 ===")
    print("=== 场景C：长对话测试 ===")

    memory = ConversationBuffer(max_rounds=3)  # 设置较小的阈值以触发压缩

    # 模拟长对话（超过压缩阈值）
    long_inputs = [
        "今天天气不错",
        "我想去公园散步",
        "公园里有好多花",
        "我看到了一只小鸟",
        "小鸟在树上唱歌",
        "我觉得很放松",
        "准备回家了",
        "今天真是美好的一天"
    ]

    total_time = 0
    for i, user_input in enumerate(long_inputs):
        start = time.perf_counter()
        response = await run_agent(user_input, memory)
        elapsed = time.perf_counter() - start
        total_time += elapsed
        logger.info(f"长对话第{i+1}轮: {elapsed:.2f}s (累计轮数: {len(memory.messages)//2})")
        print(f"长对话第{i+1}轮: {elapsed:.2f}s (累计轮数: {len(memory.messages)//2})")

    avg_time = total_time / len(long_inputs)
    logger.info(f"长对话总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    print(f"长对话总耗时: {total_time:.2f}s, 平均每轮: {avg_time:.2f}s")
    return total_time

async def main():
    print("开始性能测试...")
    logger.info("性能测试开始")

    # 运行所有测试场景
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

        print("\n性能测试完成！详细日志请查看 performance.log 文件")
        logger.info("性能测试完成")

    except Exception as e:
        logger.error(f"性能测试失败: {e}", exc_info=True)
        print(f"性能测试失败: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
