# 记忆系统

## 职责

提供长期记忆能力：从对话中自动提取事实，向量化存储，支持语义检索，带时间衰减的重要性评估。

## 核心组件

### MemoryProvider Protocol（`src/memory/base.py`）

```python
class MemoryProvider(Protocol):
    def add(self, record: MemoryRecord) -> str: ...
    def search(self, query: str, n: int = 5, ...) -> list[MemoryRecord]: ...
    def cleanup(self, min_importance: float = 0.1) -> int: ...
    def recalculate_importance(self) -> None: ...
```

### MemoryRecord（`src/memory/types.py`）

统一的记忆记录模型（Pydantic）：
- `memory_type`: FACT 或 SUMMARY
- `content`, `type_tag`, `attribute`
- `confidence`, `importance`, `access_count`
- `version`, `is_active`, `last_accessed`
- 支持 ChromaDB 序列化/反序列化

### ChromaMemoryStore（`src/memory/chroma/store.py`）

MemoryProvider 的 ChromaDB 实现：
- 单 collection 存储所有记忆类型
- 版本控制：更新时旧版本标记为 `is_active=False`
- 访问统计：搜索时自动增加 `access_count`
- 批量重新计算重要性

### FactExtractor（`src/memory/extractor.py`）

通过 LLM 从对话中提取结构化事实：
- 支持 5 大类约 25 种事实类型（user, assistant, world, conversation, interaction）
- 使用 function calling（`submit_facts` 工具）确保输出格式
- 置信度调整：模糊词降低、强确定词提高
- 过滤：敏感信息、不合理事实、低置信度

### ConversationBuffer（`src/memory/buffer.py`）

短期记忆的 token 计数滑动窗口：
- 按轮次维护对话历史
- 超出 token 上限时压缩最老的一半（通过 LLM 生成摘要，存入 ChromaDB）

### 重要性衰减（`src/memory/decay.py`）

```
importance = confidence × recency_weight × frequency_weight
```

- `recency_weight = exp(-λ × days_since_last_access)`，半衰期约 70 天
- `frequency_weight = min(1.0, log(access_count + 1) / log(20))`
- SUMMARY 类型不衰减，始终返回 1.0

### Embedding（`src/memory/chroma/embeddings.py`）

调用 Ollama API 生成向量，支持连接复用。
