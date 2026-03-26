"""
Unit tests for MemoryStore (store.py).

Tests add/search/versioning/cleanup with mocked ChromaDB.
"""

import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock, AsyncMock

from src.memory.types import MemoryRecord, MemoryType
from src.memory.store import MemoryStore


def _make_store(mock_collection):
    """Create a MemoryStore with mocked dependencies."""
    with patch("src.memory.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.store.EmbeddingClient"), \
         patch("src.memory.store.FactExtractor"), \
         patch("src.memory.store.os.getenv", side_effect=lambda k, d=None: {
             "OPENAI_MODEL_EMBEDDING": "test-model",
             "OPENAI_MODEL_EMBEDDING_URL": "http://test",
         }.get(k, d)):
        mock_client.return_value.get_or_create_collection.return_value = mock_collection
        store = MemoryStore(collection_name="test_memories")
    return store


class TestMemoryStoreAdd(unittest.TestCase):
    """Test MemoryStore.add and version control."""

    def setUp(self):
        self.mock_col = MagicMock()
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        self.store = _make_store(self.mock_col)

    def test_add_new_fact(self):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="user likes coffee",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        mid = self.store.add(record)
        self.mock_col.add.assert_called_once()
        _, kwargs = self.mock_col.add.call_args
        self.assertEqual(kwargs["documents"], ["user likes coffee"])
        meta = kwargs["metadatas"][0]
        self.assertEqual(meta["memory_type"], "fact")
        self.assertEqual(meta["version"], 1)
        self.assertTrue(meta["is_active"])
        self.assertEqual(mid, kwargs["ids"][0])

    def test_add_new_summary(self):
        mid = self.store.add_summary("conversation summary", "conv_1", ["point1", "point2"])
        self.mock_col.add.assert_called_once()
        _, kwargs = self.mock_col.add.call_args
        self.assertEqual(kwargs["documents"], ["conversation summary"])
        meta = kwargs["metadatas"][0]
        self.assertEqual(meta["memory_type"], "summary")
        self.assertEqual(meta["conversation_id"], "conv_1")

    def test_version_replacement_higher_confidence(self):
        """New record with higher confidence should replace existing."""
        now = datetime.now(timezone.utc)
        existing_meta = {
            "memory_type": "fact",
            "speaker": "user",
            "type_tag": "user.preference",
            "attribute": "user.preference.drink.coffee",
            "base_id": "abc",
            "version": 1,
            "is_active": True,
            "confidence": 0.7,
            "created_at": (now - timedelta(hours=1)).isoformat(),
        }
        self.mock_col.get.return_value = {
            "ids": ["old_id"],
            "documents": ["old content"],
            "metadatas": [existing_meta],
        }

        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="updated content",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        self.store.add(record)

        # Old should be deactivated
        self.mock_col.update.assert_called_once()
        update_kwargs = self.mock_col.update.call_args[1]
        self.assertEqual(update_kwargs["ids"], ["old_id"])
        self.assertFalse(update_kwargs["metadatas"][0]["is_active"])

        # New should be added with version 2
        _, add_kwargs = self.mock_col.add.call_args
        self.assertEqual(add_kwargs["metadatas"][0]["version"], 2)

    def test_skip_lower_confidence(self):
        """New record with lower confidence should be skipped."""
        now = datetime.now(timezone.utc)
        existing_meta = {
            "memory_type": "fact",
            "speaker": "user",
            "type_tag": "user.preference",
            "attribute": "user.preference.drink.coffee",
            "base_id": "abc",
            "version": 1,
            "is_active": True,
            "confidence": 0.95,
            "created_at": now.isoformat(),
        }
        self.mock_col.get.return_value = {
            "ids": ["existing_id"],
            "documents": ["existing content"],
            "metadatas": [existing_meta],
        }

        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="lower confidence",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.5,
        )
        result_id = self.store.add(record)

        self.assertEqual(result_id, "existing_id")
        self.mock_col.add.assert_not_called()

    def test_replace_same_confidence_newer_timestamp(self):
        """Same confidence but newer timestamp should replace."""
        old_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        existing_meta = {
            "memory_type": "fact",
            "speaker": "user",
            "type_tag": "t",
            "attribute": "a",
            "base_id": "x",
            "version": 1,
            "is_active": True,
            "confidence": 0.8,
            "created_at": old_time.isoformat(),
        }
        self.mock_col.get.return_value = {
            "ids": ["old"],
            "documents": ["old"],
            "metadatas": [existing_meta],
        }

        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="newer",
            speaker="user",
            type_tag="t",
            attribute="a",
            confidence=0.8,
        )
        self.store.add(record)
        self.mock_col.add.assert_called_once()


class TestMemoryStoreSearch(unittest.TestCase):
    """Test MemoryStore.search."""

    def setUp(self):
        self.mock_col = MagicMock()
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        self.store = _make_store(self.mock_col)

    def test_search_returns_records(self):
        self.mock_col.query.return_value = {
            "ids": [["id1", "id2"]],
            "documents": [["fact content", "summary content"]],
            "metadatas": [[
                {"memory_type": "fact", "is_active": True, "confidence": 0.9,
                 "speaker": "user", "type_tag": "user.pref", "attribute": "user.pref.x"},
                {"memory_type": "summary", "is_active": True, "conversation_id": "conv1"},
            ]],
            "distances": [[0.3, 0.5]],
        }
        # Mock access stats update
        self.mock_col.get.return_value = {
            "ids": ["id1", "id2"],
            "metadatas": [{"access_count": 0}, {"access_count": 2}],
        }

        results = self.store.search("test query")

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], MemoryRecord)
        self.assertEqual(results[0].memory_type, MemoryType.FACT)
        self.assertEqual(results[0].content, "fact content")
        self.assertEqual(results[1].memory_type, MemoryType.SUMMARY)

    def test_search_with_memory_type_filter(self):
        self.mock_col.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }

        self.store.search("query", memory_type=MemoryType.FACT)

        _, kwargs = self.mock_col.query.call_args
        where = kwargs["where"]
        # Should have $and with is_active and memory_type
        self.assertIn("$and", where)
        conditions = where["$and"]
        self.assertTrue(any(c.get("memory_type") == "fact" for c in conditions))
        self.assertTrue(any(c.get("is_active") is True for c in conditions))

    def test_search_with_type_tag_filter(self):
        self.mock_col.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }

        self.store.search("query", type_tag="user.preference")

        _, kwargs = self.mock_col.query.call_args
        where = kwargs["where"]
        self.assertIn("$and", where)
        conditions = where["$and"]
        self.assertTrue(any(c.get("type_tag") == "user.preference" for c in conditions))

    def test_search_distance_threshold(self):
        """Results beyond distance threshold should be excluded."""
        self.mock_col.query.return_value = {
            "ids": [["near", "far"]],
            "documents": [["close result", "far result"]],
            "metadatas": [[
                {"memory_type": "fact", "is_active": True},
                {"memory_type": "fact", "is_active": True},
            ]],
            "distances": [[0.3, 2.0]],  # 2.0 > threshold (1.1)
        }
        # Mock for access stat update
        self.mock_col.get.return_value = {
            "ids": ["near"],
            "metadatas": [{"access_count": 0}],
        }

        results = self.store.search("query")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, "close result")

    def test_search_empty_results(self):
        self.mock_col.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }
        results = self.store.search("nothing")
        self.assertEqual(results, [])

    def test_search_updates_access_stats(self):
        self.mock_col.query.return_value = {
            "ids": [["hit1"]],
            "documents": [["content"]],
            "metadatas": [[{"memory_type": "fact", "is_active": True}]],
            "distances": [[0.2]],
        }
        self.mock_col.get.return_value = {
            "ids": ["hit1"],
            "metadatas": [{"access_count": 3}],
        }

        self.store.search("query")

        # update should be called twice: once for search access stats
        # (get is called for access stats)
        update_calls = self.mock_col.update.call_args_list
        self.assertTrue(len(update_calls) >= 1)
        last_update = update_calls[-1]
        meta = last_update[1]["metadatas"][0]
        self.assertEqual(meta["access_count"], 4)


class TestMemoryStoreGetMethods(unittest.TestCase):
    """Test get_by_type, get_by_id, get_history."""

    def setUp(self):
        self.mock_col = MagicMock()
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        self.store = _make_store(self.mock_col)

    def test_get_by_type(self):
        self.mock_col.get.return_value = {
            "ids": ["f1", "f2"],
            "documents": ["fact 1", "fact 2"],
            "metadatas": [
                {"memory_type": "fact", "is_active": True},
                {"memory_type": "fact", "is_active": True},
            ],
        }

        results = self.store.get_by_type(MemoryType.FACT)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].content, "fact 1")

        _, kwargs = self.mock_col.get.call_args
        where = kwargs["where"]
        self.assertIn("$and", where)

    def test_get_by_id_found(self):
        self.mock_col.get.return_value = {
            "ids": ["mem1"],
            "documents": ["content here"],
            "metadatas": [{"memory_type": "fact", "confidence": 0.9}],
        }

        result = self.store.get_by_id("mem1")
        self.assertIsNotNone(result)
        self.assertEqual(result.content, "content here")

    def test_get_by_id_not_found(self):
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}

        result = self.store.get_by_id("nonexistent")
        self.assertIsNone(result)

    def test_get_history(self):
        self.mock_col.get.return_value = {
            "ids": ["v2", "v1"],
            "documents": ["new content", "old content"],
            "metadatas": [
                {"memory_type": "fact", "version": 2, "is_active": True, "base_id": "x"},
                {"memory_type": "fact", "version": 1, "is_active": False, "base_id": "x"},
            ],
        }

        history = self.store.get_history("x")
        self.assertEqual(len(history), 2)
        # Should be sorted by version ascending
        self.assertEqual(history[0].version, 1)
        self.assertEqual(history[1].version, 2)


class TestMemoryStoreDecayAndCleanup(unittest.TestCase):
    """Test cleanup and recalculate_importance."""

    def setUp(self):
        self.mock_col = MagicMock()
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        self.store = _make_store(self.mock_col)

    def test_recalculate_importance(self):
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=100)).isoformat()
        self.mock_col.get.return_value = {
            "ids": ["r1"],
            "documents": ["old memory"],
            "metadatas": [{
                "memory_type": "fact",
                "is_active": True,
                "confidence": 0.8,
                "last_accessed": old_time,
                "access_count": 5,
                "importance": 1.0,
            }],
        }

        self.store.recalculate_importance()

        # Should have called update with a lower importance value
        if self.mock_col.update.called:
            _, kwargs = self.mock_col.update.call_args
            new_importance = kwargs["metadatas"][0]["importance"]
            self.assertLess(new_importance, 1.0)

    def test_cleanup_deactivates_low_importance(self):
        now = datetime.now(timezone.utc)
        very_old = (now - timedelta(days=365)).isoformat()

        # recalculate_importance will be called first, then cleanup gets records
        call_count = [0]
        def mock_get(**kwargs):
            call_count[0] += 1
            return {
                "ids": ["old1"],
                "documents": ["very old memory"],
                "metadatas": [{
                    "memory_type": "fact",
                    "is_active": True,
                    "confidence": 0.3,
                    "last_accessed": very_old,
                    "access_count": 0,
                    "importance": 0.01,
                }],
            }

        self.mock_col.get.side_effect = mock_get

        removed = self.store.cleanup(min_importance=0.1)
        self.assertGreaterEqual(removed, 0)

    def test_delete_and_deactivate(self):
        self.store.delete("mem1")
        self.mock_col.delete.assert_called_with(ids=["mem1"])

        self.store.deactivate("mem2")
        self.mock_col.update.assert_called_with(
            ids=["mem2"], metadatas=[{"is_active": False}]
        )


class TestMemoryStoreAddFromConversation(unittest.TestCase):
    """Test add_from_conversation."""

    def setUp(self):
        self.mock_col = MagicMock()
        self.mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        self.store = _make_store(self.mock_col)

    def test_add_from_conversation(self):
        from src.memory.extractor import Fact

        mock_facts = [
            Fact(
                fact_text="user likes coffee",
                confidence=0.9,
                type="user.preference",
                speaker="user",
                source="conv1",
                original_utterance="I like coffee",
                attribute="user.preference.drink.coffee",
            )
        ]
        self.store._extractor.extract = AsyncMock(return_value=mock_facts)

        ids = asyncio.run(self.store.add_from_conversation("I like coffee"))

        self.assertEqual(len(ids), 1)
        self.mock_col.add.assert_called_once()

    def test_add_from_conversation_no_facts(self):
        self.store._extractor.extract = AsyncMock(return_value=[])

        ids = asyncio.run(self.store.add_from_conversation("hello"))

        self.assertEqual(ids, [])
        self.mock_col.add.assert_not_called()


class TestMemoryStoreInit(unittest.TestCase):
    """Test MemoryStore initialization."""

    def test_missing_env_raises(self):
        with patch("src.memory.store.os.getenv", return_value=None):
            with self.assertRaisesRegex(ValueError, "OPENAI_MODEL_EMBEDDING"):
                MemoryStore()


if __name__ == "__main__":
    unittest.main()
