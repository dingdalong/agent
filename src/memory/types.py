"""统一记忆类型系统。

MemoryType 枚举 + MemoryRecord 数据模型，替代原 memory_types.py 中的
Memory ABC、FactMemory、SummaryMemory、MemoryRegistry。
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MemoryType(StrEnum):
    FACT = "fact"
    SUMMARY = "summary"


class MemoryRecord(BaseModel):
    """所有记忆类型的统一模型。"""

    id: str = ""
    memory_type: MemoryType
    content: str  # fact_text / summary_text 统一为 content

    # 分类
    speaker: str = ""  # "user" / "assistant" / "system"
    type_tag: str = ""  # "user.preference" 等
    attribute: str = ""  # 版本合并键

    # 版本控制
    base_id: str = ""
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
    extra: dict[str, Any] = Field(default_factory=dict)

    def compute_base_id(self) -> str:
        """根据 memory_type 计算版本合并用的 base_id。"""
        if self.memory_type == MemoryType.FACT:
            raw = f"{self.speaker}|{self.type_tag}|{self.attribute}"
        elif self.memory_type == MemoryType.SUMMARY:
            raw = self.conversation_id
        else:
            raw = f"{self.memory_type}|{self.content[:50]}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def to_chroma_metadata(self) -> dict[str, str | int | float | bool]:
        """序列化为 ChromaDB 兼容的扁平 metadata。"""
        meta: dict[str, Any] = {
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
    def from_chroma(cls, doc_id: str, content: str, metadata: dict[str, Any]) -> "MemoryRecord":
        """从 ChromaDB 查询结果反序列化。"""
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
            created_at=_parse_dt(metadata.get("created_at")),
            last_accessed=_parse_dt(metadata.get("last_accessed")),
            access_count=metadata.get("access_count", 0),
            importance=metadata.get("importance", 1.0),
            conversation_id=metadata.get("conversation_id", ""),
            key_points=kp,
            original_utterance=metadata.get("original_utterance", ""),
            extra=extra,
        )


def _parse_dt(val: Any) -> datetime:
    """解析时间戳，支持 ISO 字符串和 datetime 对象。"""
    if isinstance(val, datetime):
        return val
    if isinstance(val, str) and val:
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
