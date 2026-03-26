# Memory 模块重构设计

## Context

当前 `src/memory/` 模块存在以下问题，需要彻底重构：

- **Bug**: `FactExtractor.extract()` 的 `include_types` 参数无效（prompt 在 `__init__` 固定）；时间戳用字符串比较导致格式不一致时判断错误
- **性能**: 每次调用重复创建 FactExtractor/MemorySerializer；embedding HTTP 请求无批处理无连接池；token 计算无缓存导致 O(n^2)；版本去活逐条更新
- **架构**: 两个独立 VectorMemory 实例管理复杂；`add_memory()` 接口用 `*args/**kwargs` 重载；动态修改 Enum 不安全；versioning.py/serializer.py 过度抽象
- **缺失**: 无记忆衰减/过期机制，数据无限膨胀

不考虑向后兼容，做干净重构。

## 文件结构

```
src/memory/
├── __init__.py          # 公共 API 导出
├── types.py             # MemoryType 枚举 + MemoryRecord 数据模型
├── store.py             # MemoryStore 统一存储（替代 VectorMemory）
├── buffer.py            # ConversationBuffer 短期对话管理
├── extractor.py         # FactExtractor 事实提取（修复 include_types）
├── embeddings.py        # EmbeddingClient（带连接池）
└── decay.py             # 衰减计算 + 清理接口
```

删除文件：`versioning.py`、`serializer.py`、`memory_types.py`、旧 `memory.py`、`memory_extractor.py`

## 类型系统 — `types.py`

用一个 Pydantic `MemoryRecord` 替代 Memory ABC + FactMemory + SummaryMemory + MemoryRegistry 四个类。

```python
from enum import StrEnum
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class MemoryType(StrEnum):
    FACT = "fact"
    SUMMARY = "summary"

class MemoryRecord(BaseModel):
    """所有记忆类型的统一模型"""
    id: str = ""
    memory_type: MemoryType
    content: str                    # fact_text / summary_text 统一为 content

    # 分类
    speaker: str = ""               # "user" / "assistant" / "system"
    type_tag: str = ""              # "user.preference" 等
    attribute: str = ""             # 版本合并键

    # 版本控制
    base_id: str = ""               # 自动计算的哈希
    version: int = 1
    is_active: bool = True
    confidence: float = 0.8
    source: str = ""

    # 衰减
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    access_count: int = 0
    importance: float = 1.0

    # Summary 专用
    conversation_id: str = ""
    key_points: list[str] = Field(default_factory=list)

    # 其他
    original_utterance: str = ""
    extra: dict = Field(default_factory=dict)

    def compute_base_id(self) -> str:
        """根据 memory_type 计算 base_id"""
        import hashlib
        if self.memory_type == MemoryType.FACT:
            raw = f"{self.speaker}|{self.type_tag}|{self.attribute}"
        elif self.memory_type == MemoryType.SUMMARY:
            raw = f"{self.conversation_id}"
        else:
            raw = f"{self.memory_type}|{self.content[:50]}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def to_chroma_metadata(self) -> dict[str, str | int | float | bool]:
        """序列化为 ChromaDB 兼容的扁平 metadata"""
        import json
        meta = {
            "memory_type": self.memory_type.value,
            "speaker": self.speaker,
            "type_tag": self.type_tag,
            "attribute": self.attribute,
            "base_id": self.base_id,
            "version": self.version,
            "is_active": self.is_active,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "access_count": self.access_count,
            "importance": self.importance,
            "conversation_id": self.conversation_id,
            "original_utterance": self.original_utterance,
        }
        if self.key_points:
            meta["key_points"] = json.dumps(self.key_points, ensure_ascii=False)
        if self.extra:
            meta["extra"] = json.dumps(self.extra, ensure_ascii=False)
        # 移除空字符串值以节省空间
        return {k: v for k, v in meta.items() if v != "" and v is not None}

    @classmethod
    def from_chroma(cls, doc_id: str, content: str, metadata: dict) -> "MemoryRecord":
        """从 ChromaDB 结果反序列化"""
        import json
        from datetime import datetime, timezone
        kp = metadata.get("key_points", "[]")
        if isinstance(kp, str):
            try:
                kp = json.loads(kp)
            except json.JSONDecodeError:
                kp = []
        extra = metadata.get("extra", "{}")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}

        def parse_dt(val, default=None):
            if not val:
                return default or datetime.now(timezone.utc)
            if isinstance(val, datetime):
                return val
            return datetime.fromisoformat(val)

        return cls(
            id=doc_id,
            memory_type=MemoryType(metadata.get("memory_type", "fact")),
            content=content,
            speaker=metadata.get("speaker", ""),
            type_tag=metadata.get("type_tag", ""),
            attribute=metadata.get("attribute", ""),
            base_id=metadata.get("base_id", ""),
            version=metadata.get("version", 1),
            is_active=metadata.get("is_active", True),
            confidence=metadata.get("confidence", 0.8),
            source=metadata.get("source", ""),
            created_at=parse_dt(metadata.get("created_at")),
            last_accessed=parse_dt(metadata.get("last_accessed")),
            access_count=metadata.get("access_count", 0),
            importance=metadata.get("importance", 1.0),
            conversation_id=metadata.get("conversation_id", ""),
            key_points=kp,
            original_utterance=metadata.get("original_utterance", ""),
            extra=extra,
        )
```

## 统一存储 — `store.py`

单一 `MemoryStore` 类替代两个 `VectorMemory` 实例。

```python
class MemoryStore:
    def __init__(
        self,
        collection_name: str = "memories",
        persist_dir: str = "./chroma_data",
        distance_threshold: float = 1.1,
    ):
        self._embedding = EmbeddingClient(...)      # 复用连接池
        self._extractor = FactExtractor()            # 复用实例
        self._collection = ...                       # 单一 ChromaDB collection
        self._threshold = distance_threshold

    # --- 写入 ---
    def add(self, record: MemoryRecord) -> str:
        """添加记忆，自动计算 base_id，处理版本控制"""
        # 1. record.base_id = record.compute_base_id()
        # 2. 查找同 base_id 的活跃版本
        # 3. 比较 confidence + 时间戳（用 datetime 而非字符串）
        # 4. 批量去活旧版本
        # 5. 插入新记忆

    async def add_from_conversation(
        self, user_input: str, assistant_response: str = ""
    ) -> list[str]:
        """从对话提取事实并存储"""
        facts = await self._extractor.extract(user_input, assistant_response)
        ids = []
        for fact in facts:
            record = MemoryRecord(
                memory_type=MemoryType.FACT,
                content=fact.fact_text,
                speaker=fact.speaker,
                type_tag=fact.type,
                attribute=fact.attribute,
                confidence=fact.confidence,
                source=fact.source,
                original_utterance=fact.original_utterance,
            )
            ids.append(self.add(record))
        return ids

    def add_summary(self, summary_text: str, conversation_id: str,
                    key_points: list[str] | None = None) -> str:
        """添加对话摘要"""
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content=summary_text,
            conversation_id=conversation_id,
            key_points=key_points or [],
            confidence=1.0,
        )
        return self.add(record)

    # --- 检索 ---
    def search(
        self,
        query: str,
        n: int = 5,
        memory_type: MemoryType | None = None,
        type_tag: str | None = None,
    ) -> list[MemoryRecord]:
        """语义检索，始终返回 MemoryRecord 列表"""
        # 1. 构建 where 条件 (is_active=True + 可选过滤)
        # 2. collection.query()
        # 3. 过滤 distance > threshold
        # 4. 反序列化为 MemoryRecord
        # 5. 更新命中记录的 last_accessed 和 access_count（批量）
        # 6. 返回结果

    def get_by_type(self, memory_type: MemoryType) -> list[MemoryRecord]: ...
    def get_by_id(self, memory_id: str) -> MemoryRecord | None: ...
    def get_history(self, base_id: str) -> list[MemoryRecord]: ...

    # --- 修改/删除 ---
    def delete(self, memory_id: str): ...
    def deactivate(self, memory_id: str): ...
    def clear_all(self): ...

    # --- 衰减 ---
    def cleanup(self, min_importance: float = 0.1) -> int:
        """清理低权重记忆"""
    def recalculate_importance(self):
        """批量重算所有活跃记忆的 importance"""

    # --- 内部 ---
    def _deactivate_batch(self, ids: list[str]):
        """批量去活（单次 collection.update 调用）"""
        self._collection.update(
            ids=ids,
            metadatas=[{"is_active": False}] * len(ids),
        )
```

## 对话缓冲 — `buffer.py`

从 `memory.py` 拆出 `ConversationBuffer`，关键优化：

1. **Token 缓存**：每条消息添加时计算 token 数并缓存到 `_token_cache` 列表，`_count_tokens()` 直接 `sum()`
2. **`compress()` 接受 MemoryStore**（而非 VectorMemory）
3. `print()` → `logger.info()`
4. 删除自定义 `tokenizer` 参数，内部固定用 tiktoken `cl100k_base`

```python
class ConversationBuffer:
    def __init__(self, max_rounds: int = 10, max_tokens: int = 4096,
                 system_prompt: str | None = None,
                 conversation_id: str | None = None):
        self._enc = tiktoken.get_encoding("cl100k_base")
        self._messages: list[dict] = []
        self._token_cache: list[int] = []   # 与 _messages 一一对应

    def add_user_message(self, content: str): ...
    def add_assistant_message(self, message: dict): ...
    def add_tool_message(self, tool_call_id: str, content: str): ...

    def get_messages_for_api(self) -> list[dict]:
        """返回 API 消息列表，用缓存的 token 数做截断"""

    def should_compress(self) -> bool:
        return sum(self._token_cache) > self.max_tokens

    async def compress(self, store: "MemoryStore"):
        """压缩旧消息为摘要并存入 MemoryStore"""
```

## 事实提取 — `extractor.py`

修复 `include_types` bug，简化结构：

```python
class FactExtractor:
    def __init__(self, config: ExtractorConfig | None = None):
        self.config = config or ExtractorConfig()

    async def extract(
        self,
        user_input: str,
        assistant_response: str = "",
        source_id: str | None = None,
        include_types: set[str] | None = None,
        enable_sensitive_filter: bool = True,
    ) -> list[Fact]:
        # 关键修复：每次 extract 时根据 include_types 动态构建 prompt
        target_types = self._determine_target_types(include_types)
        prompt = self._build_prompt(target_types)
        facts_data = await self._call_model(user_input, assistant_response, prompt)
        # ... 验证和构建 Fact 对象
```

改动：`_build_prompt()` 不在 `__init__` 调用，而是在 `extract()` 中按需构建。对于默认 `include_types=None` 的常见情况，缓存默认 prompt 避免重复构建。

## Embedding 客户端 — `embeddings.py`

```python
import requests

class EmbeddingClient(EmbeddingFunction):
    def __init__(self, model_name: str, base_url: str):
        self._session = requests.Session()   # 连接池复用
        self._model = model_name
        self._url = f"{base_url.rstrip('/')}/api/embeddings"

    def __call__(self, input: Documents) -> Embeddings:
        if isinstance(input, str):
            input = [input]
        embeddings = []
        for text in input:
            truncated = self._safe_truncate(text, 2048)  # token 级截断
            resp = self._session.post(self._url, json={"model": self._model, "prompt": truncated})
            resp.raise_for_status()
            embeddings.append(resp.json()["embedding"])
        return embeddings

    @staticmethod
    def _safe_truncate(text: str, max_chars: int) -> str:
        """安全截断，不截断多字节字符"""
        encoded = text.encode('utf-8')[:max_chars]
        return encoded.decode('utf-8', errors='ignore')
```

## 衰减系统 — `decay.py`

```python
import math
from datetime import datetime, timezone
from .types import MemoryRecord, MemoryType

# 可配置参数
RECENCY_LAMBDA = 0.01       # 半衰期约 70 天
FREQUENCY_CAP = 20          # access_count 达到 20 后权重不再增长

def calculate_importance(record: MemoryRecord, now: datetime | None = None) -> float:
    """
    importance = confidence_weight * recency_weight * frequency_weight

    - confidence_weight: record.confidence (0~1)
    - recency_weight: exp(-λ * days_since_last_access)
    - frequency_weight: min(1.0, log(access_count + 1) / log(FREQUENCY_CAP))

    Summary 类型不衰减，始终返回 1.0
    """
    if record.memory_type == MemoryType.SUMMARY:
        return 1.0

    now = now or datetime.now(timezone.utc)
    days = (now - record.last_accessed).total_seconds() / 86400

    confidence_w = record.confidence
    recency_w = math.exp(-RECENCY_LAMBDA * days)
    frequency_w = min(1.0, math.log(record.access_count + 1) / math.log(FREQUENCY_CAP))

    return round(confidence_w * recency_w * frequency_w, 4)
```

## 消费者变更

### main.py

```python
# Before:
user_facts = VectorMemory(collection_name=...)
conversation_summaries = VectorMemory(collection_name=...)
memory = ConversationBuffer(max_rounds=10)

# After:
from src.memory import MemoryStore, ConversationBuffer
store = MemoryStore(collection_name=_build_collection_name("memories", USER_ID))
buffer = ConversationBuffer(max_rounds=10)
```

`handle_input()` 中所有 `user_facts=user_facts, conversation_summaries=conversation_summaries` 替换为 `store=store`。

### chat.py

```python
class ChatModel(FlowModel):
    def __init__(self, memory: ConversationBuffer, store: MemoryStore, ...):
        self.memory = memory
        self.store = store    # 替代 user_facts + conversation_summaries

# on_enter_retrieving:
facts = [r.content for r in self.model.store.search(user_input, n=10, memory_type=MemoryType.FACT)]
summaries = [r.content for r in self.model.store.search(user_input, n=5, memory_type=MemoryType.SUMMARY)]

# on_enter_done:
await self.model.store.add_from_conversation(user_input)
if memory.should_compress():
    await memory.compress(self.model.store)
```

### orchestrator.py

同 chat.py 模式，`OrchestratorModel` 接受 `store: MemoryStore` 替代 `user_facts + conversation_summaries`。

## 验证方案

1. **单元测试**: 为 types.py (MemoryRecord 序列化/反序列化)、decay.py (importance 计算)、buffer.py (token 缓存、压缩) 编写测试
2. **集成测试**: MemoryStore 的 add → search → version conflict → cleanup 全流程
3. **端到端**: 启动 agent，执行多轮对话，验证：
   - 事实被正确提取和存储
   - 语义检索返回相关记忆
   - 对话压缩正常工作
   - 衰减清理能删除低权重记忆
4. **运行现有测试**: `python -m pytest tests/test_memory_types.py`（需更新为新 API）
