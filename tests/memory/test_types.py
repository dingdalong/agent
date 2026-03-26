"""
Unit tests for the new memory types module (types.py).

Tests MemoryType, MemoryRecord, serialization/deserialization.
"""

import hashlib
import json
from datetime import datetime, timezone

from src.memory.types import MemoryRecord, MemoryType


class TestMemoryType:

    def test_enum_values(self):
        assert MemoryType.FACT.value == "fact"
        assert MemoryType.SUMMARY.value == "summary"

    def test_enum_from_string(self):
        assert MemoryType("fact") == MemoryType.FACT
        assert MemoryType("summary") == MemoryType.SUMMARY


class TestMemoryRecord:

    def test_fact_creation(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="用户喜欢喝咖啡",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        assert record.memory_type == MemoryType.FACT
        assert record.content == "用户喜欢喝咖啡"
        assert record.speaker == "user"
        assert record.confidence == 0.9

    def test_summary_creation(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="这是一段对话摘要",
            conversation_id="conv_123",
            key_points=["要点1", "要点2"],
            confidence=1.0,
        )
        assert record.memory_type == MemoryType.SUMMARY
        assert record.conversation_id == "conv_123"
        assert record.key_points == ["要点1", "要点2"]

    def test_compute_base_id_fact(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="测试",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
        )
        base_id = record.compute_base_id()
        expected = hashlib.sha256("user|user.preference|user.preference.drink.coffee".encode()).hexdigest()
        assert base_id == expected

    def test_compute_base_id_summary(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="摘要",
            conversation_id="conv_123",
        )
        base_id = record.compute_base_id()
        expected = hashlib.sha256("conv_123".encode()).hexdigest()
        assert base_id == expected

    def test_consistent_hashing(self):
        """Same inputs produce same base_id."""
        r1 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a")
        r2 = MemoryRecord(memory_type=MemoryType.FACT, content="b", speaker="user", type_tag="t", attribute="a")
        assert r1.compute_base_id() == r2.compute_base_id()

    def test_different_attributes_different_hash(self):
        r1 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a1")
        r2 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a2")
        assert r1.compute_base_id() != r2.compute_base_id()

    def test_to_chroma_metadata(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="用户喜欢咖啡",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
            version=2,
            base_id="abc123",
        )
        meta = record.to_chroma_metadata()
        assert meta["memory_type"] == "fact"
        assert meta["speaker"] == "user"
        assert meta["confidence"] == 0.9
        assert meta["version"] == 2
        assert meta["base_id"] == "abc123"
        # Empty strings should be excluded
        assert "conversation_id" not in meta

    def test_to_chroma_metadata_with_key_points(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="摘要",
            conversation_id="conv_1",
            key_points=["点1", "点2"],
        )
        meta = record.to_chroma_metadata()
        assert "key_points" in meta
        parsed = json.loads(meta["key_points"])
        assert parsed == ["点1", "点2"]

    def test_to_chroma_metadata_with_extra(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="test",
            extra={"negation": True, "temporal": "present"},
        )
        meta = record.to_chroma_metadata()
        assert "extra" in meta
        parsed = json.loads(meta["extra"])
        assert parsed["negation"] is True

    def test_from_chroma_fact(self):
        metadata = {
            "memory_type": "fact",
            "speaker": "user",
            "type_tag": "user.preference",
            "attribute": "user.preference.drink.coffee",
            "confidence": 0.9,
            "version": 2,
            "base_id": "abc",
            "is_active": True,
            "created_at": "2024-01-01T12:00:00+00:00",
            "last_accessed": "2024-06-01T12:00:00+00:00",
            "access_count": 5,
            "importance": 0.85,
        }
        record = MemoryRecord.from_chroma("id1", "用户喜欢咖啡", metadata)
        assert record.id == "id1"
        assert record.memory_type == MemoryType.FACT
        assert record.content == "用户喜欢咖啡"
        assert record.speaker == "user"
        assert record.confidence == 0.9
        assert record.version == 2
        assert record.access_count == 5

    def test_from_chroma_summary_with_key_points(self):
        metadata = {
            "memory_type": "summary",
            "conversation_id": "conv_1",
            "key_points": json.dumps(["点A", "点B"]),
        }
        record = MemoryRecord.from_chroma("id2", "对话摘要", metadata)
        assert record.memory_type == MemoryType.SUMMARY
        assert record.key_points == ["点A", "点B"]

    def test_from_chroma_with_invalid_key_points(self):
        metadata = {
            "memory_type": "fact",
            "key_points": "not_valid_json{",
        }
        record = MemoryRecord.from_chroma("id3", "内容", metadata)
        assert record.key_points == []

    def test_from_chroma_missing_fields(self):
        """Missing metadata fields should use defaults."""
        record = MemoryRecord.from_chroma("id4", "内容", {})
        assert record.memory_type == MemoryType.FACT
        assert record.speaker == ""
        assert record.confidence == 0.8
        assert record.version == 1
        assert record.is_active is True

    def test_round_trip_fact(self):
        """Serialize to chroma and deserialize back."""
        original = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="用户住在北京",
            speaker="user",
            type_tag="user.personal_info",
            attribute="user.personal_info.location",
            confidence=0.85,
            source="conv1",
            original_utterance="我住在北京",
        )
        original.base_id = original.compute_base_id()
        meta = original.to_chroma_metadata()
        restored = MemoryRecord.from_chroma("test_id", original.content, meta)

        assert restored.memory_type == original.memory_type
        assert restored.content == original.content
        assert restored.speaker == original.speaker
        assert restored.type_tag == original.type_tag
        assert restored.attribute == original.attribute
        assert restored.confidence == original.confidence
        assert restored.base_id == original.base_id

    def test_round_trip_summary(self):
        original = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="对话摘要内容",
            conversation_id="conv_456",
            key_points=["要点1", "要点2", "要点3"],
            confidence=1.0,
        )
        original.base_id = original.compute_base_id()
        meta = original.to_chroma_metadata()
        restored = MemoryRecord.from_chroma("test_id", original.content, meta)

        assert restored.memory_type == original.memory_type
        assert restored.content == original.content
        assert restored.conversation_id == original.conversation_id
        assert restored.key_points == original.key_points

    def test_default_timestamps(self):
        record = MemoryRecord(memory_type=MemoryType.FACT, content="test")
        assert isinstance(record.created_at, datetime)
        assert isinstance(record.last_accessed, datetime)
        assert record.access_count == 0
        assert record.importance == 1.0
