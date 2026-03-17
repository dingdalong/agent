import json
from src.memory.memory_extractor import FactExtractor
from src.memory.memory import VectorMemory

extractor = FactExtractor()
memory = VectorMemory()

results = memory.search("名字")
print(results[0]["fact"])
# 用户第一次说“我叫小明”
memory.add_conversation(user_input="我叫小明", source_id="conv1")
results = memory.search("名字")
print(results[0]["fact"])
# 用户后来又说“我叫大明”
memory.add_conversation(user_input="我叫大明", source_id="conv1")
# 检索时只返回最新版本（is_active=True）
results = memory.search("名字")
print(results[0]["fact"])
