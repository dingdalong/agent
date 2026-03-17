"""
Unit tests for extended VectorMemory functionality.

Tests the multi-type memory support, new search capabilities,
and backward compatibility features.
"""

import unittest
import json
import uuid
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

# Import the modules to test
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.memory.memory import VectorMemory, ConversationBuffer
from src.memory.memory_types import (
    Memory, MemoryType, FactMemory, SummaryMemory, MemoryRegistry
)
from src.memory.memory_extractor import Fact
from src.memory.versioning import VersioningStrategyFactory


class TestVectorMemoryExtended(unittest.TestCase):
    """Test extended VectorMemory functionality for multi-type support."""

    def setUp(self):
        """Set up test fixtures."""
        # Clear the registry before each test
        MemoryRegistry.clear_registry()
        MemoryRegistry.register(MemoryType.FACT, FactMemory)
        MemoryRegistry.register(MemoryType.SUMMARY, SummaryMemory)

        # Mock the OllamaEmbeddingFunction
        self.mock_embedding_fn = Mock()
        self.mock_embedding_fn.return_value = [[0.1, 0.2, 0.3]]  # Simple embedding

        # Mock ChromaDB components
        self.mock_collection = Mock()
        # Set default return values for collection methods
        self.mock_collection.get.return_value = {"ids": []}  # Empty result by default
        self.mock_client = Mock()
        self.mock_client.get_or_create_collection.return_value = self.mock_collection

        # Patch the dependencies
        self.embedding_patcher = patch(
            'src.memory.memory.OllamaEmbeddingFunction',
            return_value=self.mock_embedding_fn
        )
        self.chroma_patcher = patch(
            'src.memory.memory.chromadb.PersistentClient',
            return_value=self.mock_client
        )

        self.mock_embedding_class = self.embedding_patcher.start()
        self.mock_chroma_client = self.chroma_patcher.start()

        # Create VectorMemory instance with mocked dependencies
        self.vector_memory = VectorMemory(
            collection_name="test_memories",
            persist_directory="./test_chroma"
        )

        # Mock os.getenv to return test values
        self.env_patcher = patch('src.memory.memory.os.getenv')
        self.mock_getenv = self.env_patcher.start()
        self.mock_getenv.side_effect = lambda key, default=None: {
            "OPENAI_MODEL_EMBEDDING": "test_model",
            "OPENAI_MODEL_EMBEDDING_URL": "http://test.url"
        }.get(key, default)

    def tearDown(self):
        """Tear down test fixtures."""
        self.embedding_patcher.stop()
        self.chroma_patcher.stop()
        self.env_patcher.stop()

    def test_should_compress_returns_boolean_threshold(self):
        """Test should_compress only triggers after exceeding max_tokens."""
        buffer = ConversationBuffer(max_tokens=5, tokenizer=lambda text: len(text))
        buffer.add_user_message("1234")
        self.assertFalse(buffer.should_compress())

        buffer.add_assistant_message({"role": "assistant", "content": "12"})
        self.assertTrue(buffer.should_compress())

    def test_conversation_buffer_preserves_conversation_id_in_summary_memory(self):
        """Test compress stores summaries under the buffer conversation ID."""
        buffer = ConversationBuffer(max_rounds=10, conversation_id="session-42")
        buffer.add_user_message("用户消息1")
        buffer.add_assistant_message({"role": "assistant", "content": "助手回复1"})
        buffer.add_user_message("用户消息2")
        buffer.add_assistant_message({"role": "assistant", "content": "助手回复2"})

        with patch('src.memory.memory.summarize_conversation', return_value="这是对话摘要"):
            mock_add_memory = Mock(return_value="summary_id")
            buffer.compress(vector_memory=Mock(add_memory=mock_add_memory))

        memory_arg = mock_add_memory.call_args.args[0]
        self.assertIsInstance(memory_arg, SummaryMemory)
        self.assertEqual(memory_arg.conversation_id, "session-42")
        self.assertEqual(memory_arg.metadata["session_id"], "session-42")

    def test_get_by_id_handles_missing_documents_and_metadata(self):
        """Test get_by_id gracefully handles sparse Chroma results."""
        self.mock_collection.get.return_value = {"ids": ["mem1"], "documents": None, "metadatas": None}

        result = self.vector_memory.get_by_id("mem1")

        self.assertEqual(result, {"id": "mem1", "fact": "", "metadata": {}})

    def test_vector_memory_requires_embedding_config(self):
        """Test VectorMemory raises a clear error when embedding config is missing."""
        self.mock_getenv.side_effect = lambda key, default=None: default

        with self.assertRaisesRegex(ValueError, "OPENAI_MODEL_EMBEDDING and OPENAI_MODEL_EMBEDDING_URL must be set"):
            VectorMemory(collection_name="test_memories", persist_directory="./test_chroma")

    def test_add_memory_fact_memory(self):
        """Test adding a FactMemory via add_memory method."""
        # Create a Fact
        fact = Fact(
            fact_text="用户喜欢喝茶",
            confidence=0.9,
            type="user.preference",
            speaker="user",
            source="test",
            original_utterance="我喜欢喝茶",
            attribute="user.preference.drink.tea",
            is_plausible=True,
            timestamp="2024-01-01T12:00:00Z",
            version=1,
            is_active=True,
            metadata={"additional": "data"}
        )

        # Create FactMemory
        fact_memory = FactMemory.from_fact(fact)

        # Mock collection.add to capture the call
        mock_add = self.mock_collection.add

        # Call add_memory
        memory_id = self.vector_memory.add_memory(fact_memory)

        # Verify add was called
        self.assertTrue(mock_add.called)

        # Get the arguments
        args, kwargs = mock_add.call_args

        # Verify document is the fact text
        documents = kwargs.get('documents', args[0] if args else None)
        self.assertEqual(documents, ["用户喜欢喝茶"])

        # Verify metadata contains memory_type and base_id
        metadatas = kwargs.get('metadatas', args[1] if len(args) > 1 else None)
        metadata = metadatas[0] if metadatas else {}
        self.assertIn("memory_type", metadata)
        self.assertEqual(metadata["memory_type"], "fact")
        self.assertIn("base_id", metadata)  # Should have base_id from versioning

        # Verify ID is a UUID string
        ids = kwargs.get('ids', args[2] if len(args) > 2 else None)
        self.assertIsInstance(ids[0], str)
        self.assertEqual(memory_id, ids[0])

    def test_add_memory_summary_memory(self):
        """Test adding a SummaryMemory via add_memory method."""
        # Create SummaryMemory
        summary_memory = SummaryMemory(
            summary_text="这是一段对话摘要",
            conversation_id="conv_123",
            timestamp="2024-01-01T12:00:00Z",
            key_points=["要点1", "要点2"],
            length=150
        )

        # Mock collection.add
        mock_add = self.mock_collection.add

        # Call add_memory
        memory_id = self.vector_memory.add_memory(summary_memory)

        # Verify add was called
        self.assertTrue(mock_add.called)

        # Get the arguments
        args, kwargs = mock_add.call_args

        # Verify document is the summary text
        documents = kwargs.get('documents', args[0] if args else None)
        self.assertEqual(documents, ["这是一段对话摘要"])

        # Verify metadata
        metadatas = kwargs.get('metadatas', args[1] if len(args) > 1 else None)
        metadata = metadatas[0] if metadatas else {}
        self.assertEqual(metadata["memory_type"], "summary")
        self.assertEqual(metadata["conversation_id"], "conv_123")
        self.assertIn("key_points", metadata)  # Should be JSON string

    def test_add_memory_backward_compatibility(self):
        """Test backward-compatible add_memory(content, metadata) signature."""
        # Mock empty result for _get_active_versions (no existing memories)
        self.mock_collection.get.return_value = {"ids": []}

        # Test old-style call with content and metadata
        memory_id = self.vector_memory.add_memory(
            "这是一段摘要内容",
            metadata={
                "type": "conversation_summary",
                "conversation_id": "old_conv"
            }
        )

        # Verify it was handled as SummaryMemory
        self.assertTrue(self.mock_collection.add.called)

        args, kwargs = self.mock_collection.add.call_args
        metadatas = kwargs.get('metadatas', args[1] if len(args) > 1 else None)
        metadata = metadatas[0] if metadatas else {}
        self.assertEqual(metadata["memory_type"], "summary")
        self.assertEqual(metadata["conversation_id"], "old_conv")

    def test_search_with_memory_type_filter(self):
        """Test search with memory_type filter parameter."""
        # Mock query results
        mock_results = {
            "ids": [["mem1", "mem2"]],
            "documents": [["事实内容", "摘要内容"]],
            "metadatas": [[
                {"memory_type": "fact", "type": "user.preference", "fact_text": "事实内容"},
                {"memory_type": "summary", "type": "conversation_summary", "summary_text": "摘要内容"}
            ]],
            "distances": [[0.5, 0.7]]
        }
        self.mock_collection.query.return_value = mock_results

        # Search for fact memories only
        results = self.vector_memory.search(
            query="测试查询",
            memory_type=MemoryType.FACT,
            include_inactive=True  # 新增
        )

        # Verify query was called with memory_type filter
        args, kwargs = self.mock_collection.query.call_args
        where = kwargs.get('where', {})
        self.assertIn("memory_type", where)
        self.assertEqual(where["memory_type"], "fact")

        # Should have 1 result (the fact)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["fact"], "事实内容")

    def test_search_return_memory_objects(self):
        """Test search with return_memory_objects=True."""
        # Mock query results with both fact and summary
        mock_results = {
            "ids": [["mem1", "mem2"]],
            "documents": [["用户喜欢咖啡", "对话摘要"]],
            "metadatas": [[
                {
                    "memory_type": "fact",
                    "type": "user.preference",
                    "speaker": "user",
                    "attribute": "user.preference.drink.coffee",
                    "fact_text": "用户喜欢咖啡",
                    "confidence": 0.9
                },
                {
                    "memory_type": "summary",
                    "type": "conversation_summary",
                    "conversation_id": "conv_123",
                    "summary_text": "对话摘要"
                }
            ]],
            "distances": [[0.3, 0.4]]
        }
        self.mock_collection.query.return_value = mock_results

        # Search with return_memory_objects=True
        memories = self.vector_memory.search(
            query="测试",
            return_memory_objects=True
        )

        # Should return Memory objects
        self.assertEqual(len(memories), 2)
        self.assertIsInstance(memories[0], Memory)
        self.assertIsInstance(memories[1], Memory)

        # Verify types
        self.assertEqual(memories[0].memory_type, MemoryType.FACT)
        self.assertEqual(memories[1].memory_type, MemoryType.SUMMARY)

        # Verify content
        self.assertEqual(memories[0].get_content(), "用户喜欢咖啡")
        self.assertEqual(memories[1].get_content(), "对话摘要")

    def test_search_by_type_convenience_method(self):
        """Test search_by_type convenience method."""
        # Mock query results
        mock_results = {
            "ids": [["mem1"]],
            "documents": [["事实内容"]],
            "metadatas": [[{"memory_type": "fact"}]],
            "distances": [[0.5]]
        }
        self.mock_collection.query.return_value = mock_results

        # Use search_by_type
        results = self.vector_memory.search_by_type(
            query="测试",
            memory_type=MemoryType.FACT,
            include_inactive=True  # 新增
        )

        # Verify it calls search with correct parameters
        self.mock_collection.query.assert_called_once()
        args, kwargs = self.mock_collection.query.call_args
        where = kwargs.get('where', {})
        self.assertEqual(where.get("memory_type"), "fact")

    def test_get_by_type_method(self):
        """Test get_by_type method for retrieving all memories of a type."""
        # Mock get results
        mock_results = {
            "ids": ["mem1", "mem2"],
            "documents": ["事实1", "事实2"],
            "metadatas": [
                {"memory_type": "fact", "type": "user.preference"},
                {"memory_type": "fact", "type": "user.name"}
            ]
        }
        self.mock_collection.get.return_value = mock_results

        # Get all fact memories
        facts = self.vector_memory.get_by_type(MemoryType.FACT)

        # Verify get was called with memory_type filter
        args, kwargs = self.mock_collection.get.call_args
        where = kwargs.get('where', {})
        self.assertEqual(where.get("memory_type"), "fact")

        # Should return 2 facts
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0]["fact"], "事实1")
        self.assertEqual(facts[1]["fact"], "事实2")

    def test_get_by_type_return_memory_objects(self):
        """Test get_by_type with return_memory_objects=True."""
        # Mock get results
        mock_results = {
            "ids": ["mem1"],
            "documents": ["用户喜欢喝茶"],
            "metadatas": [{
                "memory_type": "fact",
                "type": "user.preference",
                "speaker": "user",
                "attribute": "user.preference.drink.tea",
                "fact_text": "用户喜欢喝茶"
            }]
        }
        self.mock_collection.get.return_value = mock_results

        # Get as Memory objects
        memories = self.vector_memory.get_by_type(
            MemoryType.FACT,
            return_memory_objects=True
        )

        # Should return Memory objects
        self.assertEqual(len(memories), 1)
        self.assertIsInstance(memories[0], Memory)
        self.assertEqual(memories[0].memory_type, MemoryType.FACT)

    def test_backward_compatible_add_fact(self):
        """Test that _add_fact still works and is backward compatible."""
        # Create a Fact
        fact = Fact(
            fact_text="测试事实",
            confidence=0.8,
            type="test.type",
            speaker="user",
            source="test",
            original_utterance="测试",
            attribute="test.attr"
        )

        # Mock collection.add
        mock_add = self.mock_collection.add

        # Call _add_fact (should use new add_memory internally)
        memory_id = self.vector_memory._add_fact(fact)

        # Verify add was called
        self.assertTrue(mock_add.called)

        # Verify metadata contains both old and new fields
        args, kwargs = mock_add.call_args
        metadatas = kwargs.get('metadatas', args[1] if len(args) > 1 else None)
        metadata = metadatas[0] if metadatas else {}

        self.assertIn("memory_type", metadata)
        self.assertEqual(metadata["memory_type"], "fact")
        self.assertIn("base_fact_id", metadata)  # Old field for backward compatibility

    def test_backward_compatible_search_no_memory_type(self):
        """Test that search without memory_type parameter still works."""
        # Mock query results (old format without memory_type)
        mock_results = {
            "ids": [["old_mem"]],
            "documents": [["旧事实内容"]],
            "metadatas": [[{
                "type": "user.preference",
                "speaker": "user",
                "attribute": "user.preference.old"
                # No memory_type field (old data)
            }]],
            "distances": [[0.5]]
        }
        self.mock_collection.query.return_value = mock_results

        # Search without memory_type parameter (old-style call)
        results = self.vector_memory.search(query="测试")

        # Should still work
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["fact"], "旧事实内容")

    def test_search_with_both_type_filter_and_memory_type(self):
        """Test search with both old type_filter and new memory_type parameters."""
        # Mock query results
        mock_results = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        self.mock_collection.query.return_value = mock_results

        # Call with both parameters
        self.vector_memory.search(
            query="测试",
            type_filter="user.preference",  # Old parameter
            memory_type=MemoryType.FACT,     # New parameter
            include_inactive=True            # 新增
        )

        # Verify both filters are in where clause
        args, kwargs = self.mock_collection.query.call_args
        where = kwargs.get('where', {})

        # Check if where uses $and structure (for multiple conditions)
        if "$and" in where:
            # We have multiple conditions in $and format
            and_conditions = where["$and"]
            # Check that both filters are present
            type_found = any(cond.get("type") == "user.preference" for cond in and_conditions if isinstance(cond, dict))
            memory_type_found = any(cond.get("memory_type") == "fact" for cond in and_conditions if isinstance(cond, dict))
            self.assertTrue(type_found, f"type filter 'user.preference' not found in {and_conditions}")
            self.assertTrue(memory_type_found, f"memory_type filter 'fact' not found in {and_conditions}")
        else:
            # Simple dict format
            self.assertEqual(where.get("type"), "user.preference")
            self.assertEqual(where.get("memory_type"), "fact")

    def test_version_conflict_resolution_for_different_types(self):
        """Test version conflict resolution works for different memory types."""
        # Mock get_active_versions to return existing memory
        existing_memory = {
            "id": "existing_id",
            "metadata": {
                "base_id": "test_base_id",
                "version": 1,
                "confidence": 0.8,
                "timestamp": "2024-01-01T10:00:00Z"
            }
        }

        # Mock the collection.get call in _get_active_versions
        mock_get_results = {
            "ids": ["existing_id"],
            "documents": ["existing content"],
            "metadatas": [existing_memory["metadata"]]
        }

        # Reset side_effect to ensure we return the mock results
        self.mock_collection.get.side_effect = None
        self.mock_collection.get.return_value = mock_get_results

        # Create a FactMemory with higher confidence
        fact = Fact(
            fact_text="更新的事实",
            confidence=0.9,  # Higher than existing 0.8
            type="test.type",
            speaker="user",
            source="test",
            original_utterance="更新",
            attribute="test.attr",
            timestamp="2024-01-01T12:00:00Z"  # Newer timestamp
        )
        fact_memory = FactMemory.from_fact(fact)

        # Mock collection.add for new memory
        mock_add = self.mock_collection.add

        # Mock collection.update for deactivating old version
        mock_update = self.mock_collection.update

        # Add the memory (should replace existing due to higher confidence)
        self.vector_memory.add_memory(fact_memory)

        # Should deactivate old version
        self.assertTrue(mock_update.called)
        # Should add new version
        self.assertTrue(mock_add.called)

    def test_conversation_buffer_compress_with_summary_memory(self):
        """Test ConversationBuffer.compress creates and stores SummaryMemory."""
        # Create ConversationBuffer
        buffer = ConversationBuffer(max_rounds=10)

        # Add some messages
        buffer.add_user_message("用户消息1")
        buffer.add_assistant_message({"role": "assistant", "content": "助手回复1"})
        buffer.add_user_message("用户消息2")
        buffer.add_assistant_message({"role": "assistant", "content": "助手回复2"})

        # Mock summarize_conversation
        with patch('src.memory.memory.summarize_conversation') as mock_summarize:
            mock_summarize.return_value = "这是对话摘要"

            # Mock vector_memory.add_memory
            mock_add_memory = Mock(return_value="summary_id")

            # Call compress
            buffer.compress(vector_memory=Mock(add_memory=mock_add_memory))

            # Verify summarize_conversation was called
            self.assertTrue(mock_summarize.called)

            # Verify add_memory was called with SummaryMemory
            self.assertTrue(mock_add_memory.called)
            args, kwargs = mock_add_memory.call_args

            # First argument should be a Memory object
            memory_arg = args[0] if args else kwargs.get('memory')
            self.assertIsInstance(memory_arg, Memory)
            self.assertEqual(memory_arg.memory_type, MemoryType.SUMMARY)
            self.assertEqual(memory_arg.get_content(), "这是对话摘要")

    def test_error_handling_in_deserialize_memory(self):
        """Test error handling in _deserialize_memory."""
        # Test with invalid metadata (not a dict)
        memory = self.vector_memory._deserialize_memory(
            "test_id",
            "内容",
            "not_a_dict"  # Invalid metadata
        )

        # Should still return a Memory object (default to FACT)
        self.assertIsInstance(memory, Memory)

        # Test with metadata missing required fields
        memory = self.vector_memory._deserialize_memory(
            "test_id",
            "内容",
            {}  # Empty metadata
        )

        # Should default to FACT memory
        self.assertEqual(memory.memory_type, MemoryType.FACT)

    def test_memory_registry_integration(self):
        """Test that VectorMemory integrates with MemoryRegistry."""
        # Mock query results
        mock_results = {
            "ids": [["mem1"]],
            "documents": [["测试内容"]],
            "metadatas": [[{
                "memory_type": "fact",
                "fact_text": "测试内容",
                "type": "test.type",
                "speaker": "user",
                "attribute": "test.attr"
            }]],
            "distances": [[0.5]]
        }
        self.mock_collection.query.return_value = mock_results

        # Search with return_memory_objects=True
        memories = self.vector_memory.search(
            query="测试",
            return_memory_objects=True
        )

        # Memory should be created via MemoryRegistry
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].memory_type, MemoryType.FACT)

        # Verify content was properly set
        self.assertEqual(memories[0].get_content(), "测试内容")


if __name__ == '__main__':
    unittest.main()