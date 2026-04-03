"""MemoryProvider Protocol — 记忆存储的抽象接口。"""

from typing import Protocol, runtime_checkable

from src.memory.types import MemoryRecord, MemoryType


@runtime_checkable
class MemoryProvider(Protocol):
    """记忆存储的抽象接口。

    提供记忆的增删改查能力。实现者需要处理向量化存储和语义检索。
    当前实现：ChromaMemoryStore（src/memory/chroma/store.py）。

    使用方式：
        store = ChromaMemoryStore(...)
        record_id = store.add(record)
        results = store.search("用户喜欢什么", n=5)
    """

    def add(self, record: MemoryRecord) -> str: ...
    def search(
        self, query: str, n: int = 5,
        memory_type: MemoryType | None = None,
        type_tag: str | None = None,
    ) -> list[MemoryRecord]: ...
    def cleanup(self, min_importance: float = 0.1) -> int: ...
    def recalculate_importance(self) -> None: ...
