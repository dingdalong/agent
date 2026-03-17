"""
Unit tests for memory_types module.

Tests the Memory base class, FactMemory, SummaryMemory, and MemoryRegistry.
"""

import unittest
import json
from datetime import datetime, timezone
from unittest.mock import Mock, patch

# Import the modules to test
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.memory.memory_types import (
    Memory, MemoryType, FactMemory, SummaryMemory, MemoryRegistry
)
from src.memory.memory_extractor import Fact


class TestMemoryTypes(unittest.TestCase):
    """Test the Memory base class and its implementations."""

    def setUp(self):
        """Set up test fixtures."""
        # Clear the registry before each test to avoid state pollution
        MemoryRegistry.clear_registry()
        # Re-register built-in types
        MemoryRegistry.register(MemoryType.FACT, FactMemory)
        MemoryRegistry.register(MemoryType.SUMMARY, SummaryMemory)

    def test_memory_type_enum(self):
        """Test MemoryType enum values."""
        self.assertEqual(MemoryType.FACT.value, "fact")
        self.assertEqual(MemoryType.SUMMARY.value, "summary")
        self.assertIn(MemoryType.FACT, MemoryType)
        self.assertIn(MemoryType.SUMMARY, MemoryType)

    def test_memory_abstract_base_class(self):
        """Test that Memory is an abstract base class."""
        with self.assertRaises(TypeError):
            # Cannot instantiate abstract class
            Memory()

    def test_fact_memory_creation(self):
        """Test FactMemory creation and properties."""
        # Create a mock Fact object
        mock_fact = Mock(spec=Fact)
        mock_fact.fact_text = "用户喜欢喝咖啡"
        mock_fact.speaker = "user"
        mock_fact.type = "user.preference"
        mock_fact.attribute = "user.preference.drink.coffee"
        mock_fact.confidence = 0.9
        mock_fact.source = "test"
        mock_fact.original_utterance = "我喜欢喝咖啡"
        mock_fact.is_plausible = True
        mock_fact.timestamp = "2024-01-01T12:00:00Z"
        mock_fact.version = 1
        mock_fact.is_active = True
        mock_fact.fact_id = "test_fact_id"
        mock_fact.metadata = {}
        mock_fact.to_dict.return_value = {
            "fact_text": "用户喜欢喝咖啡",
            "speaker": "user",
            "type": "user.preference",
            "attribute": "user.preference.drink.coffee",
            "confidence": 0.9,
            "source": "test",
            "original_utterance": "我喜欢喝咖啡",
            "is_plausible": True,
            "timestamp": "2024-01-01T12:00:00Z",
            "version": 1,
            "is_active": True,
            "fact_id": "test_fact_id",
            "metadata": {}
        }

        # Create FactMemory
        fact_memory = FactMemory(fact=mock_fact)

        # Test properties
        self.assertEqual(fact_memory.memory_type, MemoryType.FACT)
        self.assertEqual(fact_memory.get_content(), "用户喜欢喝咖啡")

        # Test to_dict
        self.assertEqual(fact_memory.to_dict(), mock_fact.to_dict.return_value)

        # Test get_metadata
        metadata = fact_memory.get_metadata()
        self.assertIn("type", metadata)
        self.assertEqual(metadata["type"], "user.preference")
        self.assertIn("speaker", metadata)
        self.assertEqual(metadata["speaker"], "user")
        self.assertIn("attribute", metadata)
        self.assertEqual(metadata["attribute"], "user.preference.drink.coffee")
        self.assertIn("confidence", metadata)
        self.assertEqual(metadata["confidence"], 0.9)
        self.assertIn("memory_type", metadata)
        self.assertEqual(metadata["memory_type"], "fact")

    def test_fact_memory_from_fact_factory(self):
        """Test FactMemory.from_fact factory method."""
        mock_fact = Mock(spec=Fact)
        mock_fact.fact_text = "测试事实"

        fact_memory = FactMemory.from_fact(mock_fact)
        self.assertIsInstance(fact_memory, FactMemory)
        self.assertEqual(fact_memory.fact, mock_fact)

    def test_fact_memory_from_dict(self):
        """Test FactMemory.from_dict factory method."""
        data = {
            "fact_text": "用户住在北京",
            "confidence": 0.8,
            "type": "user.personal_info",
            "speaker": "user",
            "source": "test",
            "original_utterance": "我住在北京",
            "attribute": "user.personal_info.location",
            "is_plausible": True,
            "timestamp": "2024-01-01T12:00:00Z",
            "version": 1,
            "is_active": True,
            "fact_id": "test_id",
            "metadata": {}
        }

        fact_memory = FactMemory.from_dict(data)
        self.assertIsInstance(fact_memory, FactMemory)
        self.assertEqual(fact_memory.fact.fact_text, "用户住在北京")
        self.assertEqual(fact_memory.fact.speaker, "user")
        self.assertEqual(fact_memory.fact.type, "user.personal_info")

    def test_summary_memory_creation(self):
        """Test SummaryMemory creation and properties."""
        summary_memory = SummaryMemory(
            summary_text="这是一段对话摘要",
            conversation_id="conv_123",
            timestamp="2024-01-01T12:00:00Z",
            key_points=["要点1", "要点2"],
            length=100,
            metadata={"additional": "data"}
        )

        # Test properties
        self.assertEqual(summary_memory.memory_type, MemoryType.SUMMARY)
        self.assertEqual(summary_memory.get_content(), "这是一段对话摘要")
        self.assertEqual(summary_memory.conversation_id, "conv_123")
        self.assertEqual(summary_memory.timestamp, "2024-01-01T12:00:00Z")
        self.assertEqual(summary_memory.key_points, ["要点1", "要点2"])
        self.assertEqual(summary_memory.length, 100)
        self.assertEqual(summary_memory.metadata, {"additional": "data"})

        # Test to_dict
        data = summary_memory.to_dict()
        self.assertEqual(data["summary_text"], "这是一段对话摘要")
        self.assertEqual(data["conversation_id"], "conv_123")
        self.assertEqual(data["memory_type"], "summary")
        self.assertEqual(data["key_points"], ["要点1", "要点2"])
        self.assertEqual(data["length"], 100)

        # Test get_metadata
        metadata = summary_memory.get_metadata()
        self.assertEqual(metadata["type"], "conversation_summary")
        self.assertEqual(metadata["conversation_id"], "conv_123")
        self.assertEqual(metadata["memory_type"], "summary")
        # key_points should be JSON string
        self.assertIn("key_points", metadata)
        self.assertIsInstance(metadata["key_points"], str)
        parsed = json.loads(metadata["key_points"])
        self.assertEqual(parsed, ["要点1", "要点2"])

    def test_summary_memory_add_key_point(self):
        """Test SummaryMemory.add_key_point method."""
        summary_memory = SummaryMemory(
            summary_text="摘要",
            conversation_id="conv_123"
        )

        self.assertEqual(len(summary_memory.key_points), 0)
        summary_memory.add_key_point("新要点")
        self.assertEqual(len(summary_memory.key_points), 1)
        self.assertEqual(summary_memory.key_points[0], "新要点")

    def test_summary_memory_from_dict(self):
        """Test SummaryMemory.from_dict factory method."""
        # Test with list key_points
        data = {
            "summary_text": "摘要文本",
            "conversation_id": "conv_456",
            "timestamp": "2024-01-01T12:00:00Z",
            "key_points": ["点1", "点2"],
            "length": 50,
            "metadata": {"extra": "info"}
        }

        summary_memory = SummaryMemory.from_dict(data)
        self.assertIsInstance(summary_memory, SummaryMemory)
        self.assertEqual(summary_memory.summary_text, "摘要文本")
        self.assertEqual(summary_memory.key_points, ["点1", "点2"])

        # Test with JSON string key_points
        data_str = {
            "summary_text": "摘要文本",
            "conversation_id": "conv_456",
            "key_points": json.dumps(["点A", "点B"])
        }

        summary_memory = SummaryMemory.from_dict(data_str)
        self.assertEqual(summary_memory.key_points, ["点A", "点B"])

    def test_memory_registry(self):
        """Test MemoryRegistry functionality."""
        # Test get_registered_types
        types = MemoryRegistry.get_registered_types()
        self.assertIn("fact", types)
        self.assertIn("summary", types)

        # Test create method
        # Create a FactMemory via registry
        mock_fact = Mock(spec=Fact)
        mock_fact.fact_text = "测试"

        fact_memory = MemoryRegistry.create(MemoryType.FACT, fact=mock_fact)
        self.assertIsInstance(fact_memory, FactMemory)
        self.assertEqual(fact_memory.memory_type, MemoryType.FACT)

        # Create a SummaryMemory via registry
        summary_memory = MemoryRegistry.create(
            MemoryType.SUMMARY,
            summary_text="测试摘要",
            conversation_id="test_conv"
        )
        self.assertIsInstance(summary_memory, SummaryMemory)
        self.assertEqual(summary_memory.memory_type, MemoryType.SUMMARY)

        # Test from_dict method
        data_fact = {
            "memory_type": "fact",
            "fact_text": "测试事实",
            "speaker": "user",
            "type": "user.test",
            "attribute": "user.test.attr",
            "confidence": 0.8,
            "source": "test",
            "original_utterance": "测试"
        }

        fact_from_dict = MemoryRegistry.from_dict(data_fact)
        self.assertIsInstance(fact_from_dict, FactMemory)

        data_summary = {
            "memory_type": "summary",
            "summary_text": "测试摘要",
            "conversation_id": "test_conv"
        }

        summary_from_dict = MemoryRegistry.from_dict(data_summary)
        self.assertIsInstance(summary_from_dict, SummaryMemory)

    def test_memory_registry_type_inference(self):
        """Test MemoryRegistry.from_dict type inference."""
        # Infer FACT from fact_text field
        data_no_type = {
            "fact_text": "用户喜欢茶",
            "speaker": "user",
            "type": "user.preference",
            "attribute": "user.preference.drink.tea"
        }

        memory = MemoryRegistry.from_dict(data_no_type)
        self.assertIsInstance(memory, FactMemory)

        # Infer SUMMARY from summary_text field
        data_no_type2 = {
            "summary_text": "摘要",
            "conversation_id": "conv_123"
        }

        memory = MemoryRegistry.from_dict(data_no_type2)
        self.assertIsInstance(memory, SummaryMemory)

        # Should raise error if cannot infer
        data_ambiguous = {
            "content": "未知内容"
        }

        with self.assertRaises(ValueError):
            MemoryRegistry.from_dict(data_ambiguous)

    def test_memory_registry_register_new_type(self):
        """Test registering a new memory type."""
        # Define a custom memory type
        class CustomMemory(Memory):
            def __init__(self, custom_field: str):
                self.custom_field = custom_field

            @property
            def memory_type(self):
                return MemoryType("custom")  # Not in enum, but allowed

            def to_dict(self):
                return {"custom_field": self.custom_field, "memory_type": "custom"}

            def get_content(self):
                return self.custom_field

            def get_metadata(self):
                return {"custom_field": self.custom_field, "memory_type": "custom"}

        # Register the custom type
        MemoryRegistry.register("custom", CustomMemory)

        # Verify it's registered
        self.assertIn("custom", MemoryRegistry.get_registered_types())

        # Create via registry
        custom_memory = MemoryRegistry.create("custom", custom_field="测试自定义")
        self.assertIsInstance(custom_memory, CustomMemory)
        self.assertEqual(custom_memory.custom_field, "测试自定义")

    def test_memory_repr(self):
        """Test Memory.__repr__ method."""
        mock_fact = Mock(spec=Fact)
        mock_fact.fact_text = "这是一段比较长的测试文本用于测试repr方法的截断功能"

        fact_memory = FactMemory(fact=mock_fact)
        repr_str = repr(fact_memory)

        self.assertIn("FactMemory", repr_str)
        self.assertIn("type=MemoryType.FACT", repr_str)
        # Should be truncated to 50 chars
        self.assertIn("这是一段比较长的测试文本用于测试repr方法的截断功能", repr_str[:70])

    def test_summary_memory_round_trip(self):
        """Test SummaryMemory round-trip serialization/deserialization (regression for P1)."""
        # Create a SummaryMemory with various fields
        original = SummaryMemory(
            summary_text="这是一段对话摘要，包含多个关键点。",
            conversation_id="conv_12345",
            timestamp="2024-01-01T12:00:00Z",
            key_points=["要点1", "要点2", "要点3"],
            length=150,
            metadata={"source": "test", "additional": "info"}
        )

        # Convert to dictionary
        data = original.to_dict()

        # Ensure memory_type is included
        self.assertEqual(data["memory_type"], "summary")

        # Create new instance from dictionary
        restored = SummaryMemory.from_dict(data)

        # Compare fields
        self.assertEqual(restored.summary_text, original.summary_text)
        self.assertEqual(restored.conversation_id, original.conversation_id)
        self.assertEqual(restored.timestamp, original.timestamp)
        self.assertEqual(restored.key_points, original.key_points)
        self.assertEqual(restored.length, original.length)
        self.assertEqual(restored.metadata, original.metadata)

        # Ensure get_content matches
        self.assertEqual(restored.get_content(), original.get_content())

        # Ensure get_metadata matches (note: key_points are JSON string in metadata)
        original_metadata = original.get_metadata()
        restored_metadata = restored.get_metadata()
        self.assertEqual(restored_metadata["type"], original_metadata["type"])
        self.assertEqual(restored_metadata["conversation_id"], original_metadata["conversation_id"])
        self.assertEqual(restored_metadata["timestamp"], original_metadata["timestamp"])
        self.assertEqual(restored_metadata["length"], original_metadata["length"])
        # key_points are JSON strings, compare parsed
        import json
        self.assertEqual(
            json.loads(restored_metadata["key_points"]),
            json.loads(original_metadata["key_points"])
        )


if __name__ == '__main__':
    unittest.main()