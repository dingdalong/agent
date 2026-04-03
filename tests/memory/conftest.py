"""Memory module test fixtures."""
import pytest
from unittest.mock import MagicMock, patch

from src.memory.chroma.store import ChromaMemoryStore


@pytest.fixture
def mock_chroma_collection():
    """Create a mocked ChromaDB collection."""
    col = MagicMock()
    col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    return col


@pytest.fixture
def memory_store(mock_chroma_collection):
    """Create a ChromaMemoryStore with mocked dependencies."""
    with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.chroma.store.EmbeddingClient"), \
         patch("src.memory.chroma.store.FactExtractor"):
        mock_client.return_value.get_or_create_collection.return_value = mock_chroma_collection
        store = ChromaMemoryStore(
            embedding_model="test-model",
            embedding_url="http://test",
            collection_name="test_memories",
        )
    return store


@pytest.fixture
def chroma_memory_store(mock_chroma_collection):
    """Create a ChromaMemoryStore with mocked dependencies."""
    with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.chroma.store.EmbeddingClient"), \
         patch("src.memory.chroma.store.FactExtractor"):
        mock_client.return_value.get_or_create_collection.return_value = mock_chroma_collection
        store = ChromaMemoryStore(
            embedding_model="test-model",
            embedding_url="http://test",
            collection_name="test_memories",
        )
    return store
