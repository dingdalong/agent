"""
Unit tests for the new memory types module (types.py).

Tests MemoryType, MemoryRecord, serialization/deserialization.
"""

import hashlib
import json
import unittest
from datetime import datetime, timezone

from src.memory.types import MemoryRecord, MemoryType


class TestMemoryType(unittest.TestCase):

    def test_enum_values(self):
        self.assertEqual(MemoryType.FACT.value, "fact")
        self.assertEqual(MemoryType.SUMMARY.value, "summary")

    def test_enum_from_string(self):
        self.assertEqual(MemoryType("fact"), MemoryType.FACT)
        self.assertEqual(MemoryType("summary"), MemoryType.SUMMARY)


class TestMemoryRecord(unittest.TestCase):

    def test_fact_creation(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="用户喜欢喝咖啡",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        self.assertEqual(record.memory_type, MemoryType.FACT)
        self.assertEqual(record.content, "用户喜欢喝咖啡")
        self.assertEqual(record.speaker, "user")
        self.assertEqual(record.confidence, 0.9)

    def test_summary_creation(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="这是一段对话摘要",
            conversation_id="conv_123",
            key_points=["要点1", "要点2"],
            confidence=1.0,
        )
        self.assertEqual(record.memory_type, MemoryType.SUMMARY)
        self.assertEqual(record.conversation_id, "conv_123")
        self.assertEqual(record.key_points, ["要点1", "要点2"])

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
        self.assertEqual(base_id, expected)

    def test_compute_base_id_summary(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="摘要",
            conversation_id="conv_123",
        )
        base_id = record.compute_base_id()
        expected = hashlib.sha256("conv_123".encode()).hexdigest()
        self.assertEqual(base_id, expected)

    def test_consistent_hashing(self):
        """Same inputs produce same base_id."""
        r1 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a")
        r2 = MemoryRecord(memory_type=MemoryType.FACT, content="b", speaker="user", type_tag="t", attribute="a")
        self.assertEqual(r1.compute_base_id(), r2.compute_base_id())

    def test_different_attributes_different_hash(self):
        r1 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a1")
        r2 = MemoryRecord(memory_type=MemoryType.FACT, content="a", speaker="user", type_tag="t", attribute="a2")
        self.assertNotEqual(r1.compute_base_id(), r2.compute_base_id())

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
        self.assertEqual(meta["memory_type"], "fact")
        self.assertEqual(meta["speaker"], "user")
        self.assertEqual(meta["confidence"], 0.9)
        self.assertEqual(meta["version"], 2)
        self.assertEqual(meta["base_id"], "abc123")
        # Empty strings should be excluded
        self.assertNotIn("conversation_id", meta)

    def test_to_chroma_metadata_with_key_points(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="摘要",
            conversation_id="conv_1",
            key_points=["点1", "点2"],
        )
        meta = record.to_chroma_metadata()
        self.assertIn("key_points", meta)
        parsed = json.loads(meta["key_points"])
        self.assertEqual(parsed, ["点1", "点2"])

    def test_to_chroma_metadata_with_extra(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="test",
            extra={"negation": True, "temporal": "present"},
        )
        meta = record.to_chroma_metadata()
        self.assertIn("extra", meta)
        parsed = json.loads(meta["extra"])
        self.assertEqual(parsed["negation"], True)

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
        self.assertEqual(record.id, "id1")
        self.assertEqual(record.memory_type, MemoryType.FACT)
        self.assertEqual(record.content, "用户喜欢咖啡")
        self.assertEqual(record.speaker, "user")
        self.assertEqual(record.confidence, 0.9)
        self.assertEqual(record.version, 2)
        self.assertEqual(record.access_count, 5)

    def test_from_chroma_summary_with_key_points(self):
        metadata = {
            "memory_type": "summary",
            "conversation_id": "conv_1",
            "key_points": json.dumps(["点A", "点B"]),
        }
        record = MemoryRecord.from_chroma("id2", "对话摘要", metadata)
        self.assertEqual(record.memory_type, MemoryType.SUMMARY)
        self.assertEqual(record.key_points, ["点A", "点B"])

    def test_from_chroma_with_invalid_key_points(self):
        metadata = {
            "memory_type": "fact",
            "key_points": "not_valid_json{",
        }
        record = MemoryRecord.from_chroma("id3", "内容", metadata)
        self.assertEqual(record.key_points, [])

    def test_from_chroma_missing_fields(self):
        """Missing metadata fields should use defaults."""
        record = MemoryRecord.from_chroma("id4", "内容", {})
        self.assertEqual(record.memory_type, MemoryType.FACT)
        self.assertEqual(record.speaker, "")
        self.assertEqual(record.confidence, 0.8)
        self.assertEqual(record.version, 1)
        self.assertTrue(record.is_active)

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

        self.assertEqual(restored.memory_type, original.memory_type)
        self.assertEqual(restored.content, original.content)
        self.assertEqual(restored.speaker, original.speaker)
        self.assertEqual(restored.type_tag, original.type_tag)
        self.assertEqual(restored.attribute, original.attribute)
        self.assertEqual(restored.confidence, original.confidence)
        self.assertEqual(restored.base_id, original.base_id)

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

        self.assertEqual(restored.memory_type, original.memory_type)
        self.assertEqual(restored.content, original.content)
        self.assertEqual(restored.conversation_id, original.conversation_id)
        self.assertEqual(restored.key_points, original.key_points)

    def test_default_timestamps(self):
        record = MemoryRecord(memory_type=MemoryType.FACT, content="test")
        self.assertIsInstance(record.created_at, datetime)
        self.assertIsInstance(record.last_accessed, datetime)
        self.assertEqual(record.access_count, 0)
        self.assertEqual(record.importance, 1.0)


if __name__ == "__main__":
    unittest.main()
