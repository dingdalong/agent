"""Memory module test fixtures."""
import pytest
from unittest.mock import MagicMock, patch

from src.memory.store import MemoryStore
from src.memory.chroma.store import ChromaMemoryStore


@pytest.fixture
def mock_chroma_collection():
    """Create a mocked ChromaDB collection."""
    col = MagicMock()
    col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    return col


@pytest.fixture
def memory_store(mock_chroma_collection):
    """Create a MemoryStore with mocked dependencies."""
    with patch("src.memory.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.store.EmbeddingClient"), \
         patch("src.memory.store.FactExtractor"), \
         patch("src.memory.store.os.getenv", side_effect=lambda k, d=None: {
             "OPENAI_MODEL_EMBEDDING": "test-model",
             "OPENAI_MODEL_EMBEDDING_URL": "http://test",
         }.get(k, d)):
        mock_client.return_value.get_or_create_collection.return_value = mock_chroma_collection
        store = MemoryStore(collection_name="test_memories")
    return store


@pytest.fixture
def chroma_memory_store(mock_chroma_collection):
    """Create a ChromaMemoryStore with mocked dependencies."""
    with patch("src.memory.chroma.store.chromadb.PersistentClient") as mock_client, \
         patch("src.memory.chroma.store.EmbeddingClient"), \
         patch("src.memory.chroma.store.FactExtractor"), \
         patch("src.memory.chroma.store.os.getenv", side_effect=lambda k, d=None: {
             "OPENAI_MODEL_EMBEDDING": "test-model",
             "OPENAI_MODEL_EMBEDDING_URL": "http://test",
         }.get(k, d)):
        mock_client.return_value.get_or_create_collection.return_value = mock_chroma_collection
        store = ChromaMemoryStore(collection_name="test_memories")
    return store
