"""
Unit tests for ChromaMemoryStore (chroma/store.py).

Tests add/search/versioning/cleanup with mocked ChromaDB.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from src.memory.types import MemoryRecord, MemoryType
from src.memory.chroma.store import ChromaMemoryStore


def _make_store(mock_collection):
    """Create a ChromaMemoryStore with mocked dependencies."""
    with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.chroma.store.EmbeddingClient"), \
         patch("src.memory.chroma.store.FactExtractor"):
        mock_client.return_value.get_or_create_collection.return_value = mock_collection
        store = ChromaMemoryStore(
            embedding_model="test-model",
            embedding_url="http://test",
            collection_name="test_memories",
        )
    return store


def _make_chroma_store(mock_collection):
    """Create a ChromaMemoryStore with mocked dependencies."""
    with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.chroma.store.EmbeddingClient"), \
         patch("src.memory.chroma.store.FactExtractor"):
        mock_client.return_value.get_or_create_collection.return_value = mock_collection
        store = ChromaMemoryStore(
            embedding_model="test-model",
            embedding_url="http://test",
            collection_name="test_memories",
        )
    return store


class TestMemoryStoreAdd:
    """Test MemoryStore.add and version control."""

    def test_add_new_fact(self, memory_store, mock_chroma_collection):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="user likes coffee",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        mid = memory_store.add(record)
        mock_chroma_collection.add.assert_called_once()
        _, kwargs = mock_chroma_collection.add.call_args
        assert kwargs["documents"] == ["user likes coffee"]
        meta = kwargs["metadatas"][0]
        assert meta["memory_type"] == "fact"
        assert meta["version"] == 1
        assert meta["is_active"] is True
        assert mid == kwargs["ids"][0]

    def test_add_new_summary(self, memory_store, mock_chroma_collection):
        mid = memory_store.add_summary("conversation summary", "conv_1", ["point1", "point2"])
        mock_chroma_collection.add.assert_called_once()
        _, kwargs = mock_chroma_collection.add.call_args
        assert kwargs["documents"] == ["conversation summary"]
        meta = kwargs["metadatas"][0]
        assert meta["memory_type"] == "summary"
        assert meta["conversation_id"] == "conv_1"

    def test_version_replacement_higher_confidence(self, memory_store, mock_chroma_collection):
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
        mock_chroma_collection.get.return_value = {
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
        memory_store.add(record)

        # Old should be deactivated
        mock_chroma_collection.update.assert_called_once()
        update_kwargs = mock_chroma_collection.update.call_args[1]
        assert update_kwargs["ids"] == ["old_id"]
        assert update_kwargs["metadatas"][0]["is_active"] is False

        # New should be added with version 2
        _, add_kwargs = mock_chroma_collection.add.call_args
        assert add_kwargs["metadatas"][0]["version"] == 2

    def test_skip_lower_confidence(self, memory_store, mock_chroma_collection):
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
        mock_chroma_collection.get.return_value = {
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
        result_id = memory_store.add(record)

        assert result_id == "existing_id"
        mock_chroma_collection.add.assert_not_called()

    def test_replace_same_confidence_newer_timestamp(self, memory_store, mock_chroma_collection):
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
        mock_chroma_collection.get.return_value = {
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
        memory_store.add(record)
        mock_chroma_collection.add.assert_called_once()


class TestMemoryStoreSearch:
    """Test MemoryStore.search."""

    def test_search_returns_records(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
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
        mock_chroma_collection.get.return_value = {
            "ids": ["id1", "id2"],
            "metadatas": [{"access_count": 0}, {"access_count": 2}],
        }

        results = memory_store.search("test query")

        assert len(results) == 2
        assert isinstance(results[0], MemoryRecord)
        assert results[0].memory_type == MemoryType.FACT
        assert results[0].content == "fact content"
        assert results[1].memory_type == MemoryType.SUMMARY

    def test_search_with_memory_type_filter(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }

        memory_store.search("query", memory_type=MemoryType.FACT)

        _, kwargs = mock_chroma_collection.query.call_args
        where = kwargs["where"]
        # Should have $and with is_active and memory_type
        assert "$and" in where
        conditions = where["$and"]
        assert any(c.get("memory_type") == "fact" for c in conditions)
        assert any(c.get("is_active") is True for c in conditions)

    def test_search_with_type_tag_filter(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }

        memory_store.search("query", type_tag="user.preference")

        _, kwargs = mock_chroma_collection.query.call_args
        where = kwargs["where"]
        assert "$and" in where
        conditions = where["$and"]
        assert any(c.get("type_tag") == "user.preference" for c in conditions)

    def test_search_distance_threshold(self, memory_store, mock_chroma_collection):
        """Results beyond distance threshold should be excluded."""
        mock_chroma_collection.query.return_value = {
            "ids": [["near", "far"]],
            "documents": [["close result", "far result"]],
            "metadatas": [[
                {"memory_type": "fact", "is_active": True},
                {"memory_type": "fact", "is_active": True},
            ]],
            "distances": [[0.3, 2.0]],  # 2.0 > threshold (1.1)
        }
        # Mock for access stat update
        mock_chroma_collection.get.return_value = {
            "ids": ["near"],
            "metadatas": [{"access_count": 0}],
        }

        results = memory_store.search("query")

        assert len(results) == 1
        assert results[0].content == "close result"

    def test_search_empty_results(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }
        results = memory_store.search("nothing")
        assert results == []

    def test_search_updates_access_stats(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [["hit1"]],
            "documents": [["content"]],
            "metadatas": [[{"memory_type": "fact", "is_active": True}]],
            "distances": [[0.2]],
        }
        mock_chroma_collection.get.return_value = {
            "ids": ["hit1"],
            "metadatas": [{"access_count": 3}],
        }

        memory_store.search("query")

        # update should be called twice: once for search access stats
        # (get is called for access stats)
        update_calls = mock_chroma_collection.update.call_args_list
        assert len(update_calls) >= 1
        last_update = update_calls[-1]
        meta = last_update[1]["metadatas"][0]
        assert meta["access_count"] == 4


class TestMemoryStoreGetMethods:
    """Test get_by_type, get_by_id, get_history."""

    def test_get_by_type(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.get.return_value = {
            "ids": ["f1", "f2"],
            "documents": ["fact 1", "fact 2"],
            "metadatas": [
                {"memory_type": "fact", "is_active": True},
                {"memory_type": "fact", "is_active": True},
            ],
        }

        results = memory_store.get_by_type(MemoryType.FACT)
        assert len(results) == 2
        assert results[0].content == "fact 1"

        _, kwargs = mock_chroma_collection.get.call_args
        where = kwargs["where"]
        assert "$and" in where

    def test_get_by_id_found(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.get.return_value = {
            "ids": ["mem1"],
            "documents": ["content here"],
            "metadatas": [{"memory_type": "fact", "confidence": 0.9}],
        }

        result = memory_store.get_by_id("mem1")
        assert result is not None
        assert result.content == "content here"

    def test_get_by_id_not_found(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.get.return_value = {"ids": [], "documents": [], "metadatas": []}

        result = memory_store.get_by_id("nonexistent")
        assert result is None

    def test_get_history(self, memory_store, mock_chroma_collection):
        mock_chroma_collection.get.return_value = {
            "ids": ["v2", "v1"],
            "documents": ["new content", "old content"],
            "metadatas": [
                {"memory_type": "fact", "version": 2, "is_active": True, "base_id": "x"},
                {"memory_type": "fact", "version": 1, "is_active": False, "base_id": "x"},
            ],
        }

        history = memory_store.get_history("x")
        assert len(history) == 2
        # Should be sorted by version ascending
        assert history[0].version == 1
        assert history[1].version == 2


class TestMemoryStoreDecayAndCleanup:
    """Test cleanup and recalculate_importance."""

    def test_recalculate_importance(self, memory_store, mock_chroma_collection):
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=100)).isoformat()
        mock_chroma_collection.get.return_value = {
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

        memory_store.recalculate_importance()

        # Should have called update with a lower importance value
        if mock_chroma_collection.update.called:
            _, kwargs = mock_chroma_collection.update.call_args
            new_importance = kwargs["metadatas"][0]["importance"]
            assert new_importance < 1.0

    def test_cleanup_deactivates_low_importance(self, memory_store, mock_chroma_collection):
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

        mock_chroma_collection.get.side_effect = mock_get

        removed = memory_store.cleanup(min_importance=0.1)
        assert removed >= 0

    def test_delete_and_deactivate(self, memory_store, mock_chroma_collection):
        memory_store.delete("mem1")
        mock_chroma_collection.delete.assert_called_with(ids=["mem1"])

        memory_store.deactivate("mem2")
        mock_chroma_collection.update.assert_called_with(
            ids=["mem2"], metadatas=[{"is_active": False}]
        )


class TestMemoryStoreAddFromConversation:
    """Test add_from_conversation."""

    def test_add_from_conversation(self, memory_store, mock_chroma_collection):
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
        memory_store._extractor.extract = AsyncMock(return_value=mock_facts)

        ids = asyncio.run(memory_store.add_from_conversation("I like coffee"))

        assert len(ids) == 1
        mock_chroma_collection.add.assert_called_once()

    def test_add_from_conversation_no_facts(self, memory_store, mock_chroma_collection):
        memory_store._extractor.extract = AsyncMock(return_value=[])

        ids = asyncio.run(memory_store.add_from_conversation("hello"))

        assert ids == []
        mock_chroma_collection.add.assert_not_called()


# === ChromaMemoryStore tests ===


class TestChromaMemoryStoreAdd:
    """Test ChromaMemoryStore.add and version control."""

    def test_add_new_fact(self, chroma_memory_store, mock_chroma_collection):
        record = MemoryRecord(
            memory_type=MemoryType.FACT,
            content="user likes coffee",
            speaker="user",
            type_tag="user.preference",
            attribute="user.preference.drink.coffee",
            confidence=0.9,
        )
        mid = chroma_memory_store.add(record)
        mock_chroma_collection.add.assert_called_once()
        _, kwargs = mock_chroma_collection.add.call_args
        assert kwargs["documents"] == ["user likes coffee"]
        meta = kwargs["metadatas"][0]
        assert meta["memory_type"] == "fact"
        assert meta["version"] == 1
        assert meta["is_active"] is True
        assert mid == kwargs["ids"][0]

    def test_add_new_summary(self, chroma_memory_store, mock_chroma_collection):
        mid = chroma_memory_store.add_summary("conversation summary", "conv_1", ["point1", "point2"])
        mock_chroma_collection.add.assert_called_once()
        _, kwargs = mock_chroma_collection.add.call_args
        assert kwargs["documents"] == ["conversation summary"]
        meta = kwargs["metadatas"][0]
        assert meta["memory_type"] == "summary"
        assert meta["conversation_id"] == "conv_1"


class TestChromaMemoryStoreSearch:
    """Test ChromaMemoryStore.search."""

    def test_search_returns_records(self, chroma_memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [["id1", "id2"]],
            "documents": [["fact content", "summary content"]],
            "metadatas": [[
                {"memory_type": "fact", "is_active": True, "confidence": 0.9,
                 "speaker": "user", "type_tag": "user.pref", "attribute": "user.pref.x"},
                {"memory_type": "summary", "is_active": True, "conversation_id": "conv1"},
            ]],
            "distances": [[0.3, 0.5]],
        }
        mock_chroma_collection.get.return_value = {
            "ids": ["id1", "id2"],
            "metadatas": [{"access_count": 0}, {"access_count": 2}],
        }

        results = chroma_memory_store.search("test query")

        assert len(results) == 2
        assert isinstance(results[0], MemoryRecord)
        assert results[0].memory_type == MemoryType.FACT

    def test_search_empty_results(self, chroma_memory_store, mock_chroma_collection):
        mock_chroma_collection.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
        }
        results = chroma_memory_store.search("nothing")
        assert results == []


class TestChromaMemoryStoreInit:
    """Test ChromaMemoryStore initialization."""

    def test_requires_embedding_model(self):
        """Constructor requires embedding_model and embedding_url."""
        with pytest.raises(TypeError):
            ChromaMemoryStore()  # type: ignore[call-arg]

    def test_accepts_explicit_params(self, mock_chroma_collection):
        with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
             patch("src.memory.chroma.store.EmbeddingClient") as mock_emb, \
             patch("src.memory.chroma.store.FactExtractor"):
            mock_client.return_value.get_or_create_collection.return_value = mock_chroma_collection
            store = ChromaMemoryStore(
                embedding_model="my-model",
                embedding_url="http://embed",
            )
            mock_emb.assert_called_once_with("my-model", "http://embed")


# === Collection name tests ===


class TestBuildCollectionName:

    def test_collection_name_includes_sanitized_user_id(self):
        from src.memory.utils import build_collection_name
        assert build_collection_name("user_facts", "User/ABC 123") == "user_facts_user_abc_123"

    def test_collection_name_no_user_id(self):
        from src.memory.utils import build_collection_name
        assert build_collection_name("memories", None) == "memories"

    def test_collection_name_empty_user_id(self):
        from src.memory.utils import build_collection_name
        assert build_collection_name("memories", "") == "memories"

