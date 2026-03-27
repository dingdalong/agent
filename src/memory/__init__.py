"""Memory system for AI Agent.

Components:
- MemoryProvider: 记忆存储的抽象协议
- ChromaMemoryStore: ChromaDB 实现
- MemoryStore: 旧名称（保留兼容）
- ConversationBuffer: 短期对话缓冲（带 token 缓存）
- MemoryRecord / MemoryType: 统一记忆数据模型
- FactExtractor: 从对话中提取结构化事实
- calculate_importance: 记忆衰减权重计算
"""

__all__ = [
    "MemoryProvider",
    "ChromaMemoryStore",
    "MemoryStore",
    "ConversationBuffer",
    "MemoryRecord",
    "MemoryType",
    "FactExtractor",
    "Fact",
    "ExtractorConfig",
    "EmbeddingClient",
    "calculate_importance",
    "summarize_conversation",
]

from .base import MemoryProvider
from .buffer import ConversationBuffer, summarize_conversation
from .chroma import ChromaMemoryStore
from .decay import calculate_importance
from .embeddings import EmbeddingClient
from .extractor import ExtractorConfig, Fact, FactExtractor
from .store import MemoryStore
from .types import MemoryRecord, MemoryType
