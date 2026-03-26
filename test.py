import asyncio
import json
from src.memory import FactExtractor, MemoryStore

async def main():
    extractor = FactExtractor()
    store = MemoryStore()

    results = store.search("名字")
    print(results[0].content if results else "无结果")

    # 用户第一次说"我叫小明"
    await store.add_from_conversation(user_input="我叫小明", source_id="conv1")
    results = store.search("名字")
    print(results[0].content if results else "无结果")

    # 用户后来又说"我叫大明"
    await store.add_from_conversation(user_input="我叫大明", source_id="conv1")
    # 检索时只返回最新版本（is_active=True）
    results = store.search("名字")
    print(results[0].content if results else "无结果")

if __name__ == "__main__":
    asyncio.run(main())
