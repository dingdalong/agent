"""Memory system for AI Agent.

Components:
- MemoryStore: 统一向量记忆存储（单 collection，支持版本控制和衰减）
- ConversationBuffer: 短期对话缓冲（带 token 缓存）
- MemoryRecord / MemoryType: 统一记忆数据模型
- FactExtractor: 从对话中提取结构化事实
- calculate_importance: 记忆衰减权重计算
"""

__all__ = [
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

from .buffer import ConversationBuffer, summarize_conversation
from .decay import calculate_importance
from .embeddings import EmbeddingClient
from .extractor import ExtractorConfig, Fact, FactExtractor
from .store import MemoryStore
from .types import MemoryRecord, MemoryType
