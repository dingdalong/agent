"""MemoryProvider Protocol — 记忆存储的抽象接口。"""

from typing import Protocol

from src.memory.types import MemoryRecord, MemoryType


class MemoryProvider(Protocol):
    """所有记忆存储实现必须满足的协议。"""

    def add(self, record: MemoryRecord) -> str: ...
    def search(
        self, query: str, n: int = 5,
        memory_type: MemoryType | None = None,
        type_tag: str | None = None,
    ) -> list[MemoryRecord]: ...
    def cleanup(self, min_importance: float = 0.1) -> int: ...
    def recalculate_importance(self) -> None: ...
