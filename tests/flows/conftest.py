"""Flow test fixtures."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_io():
    """Mock agent_input and agent_output for flow testing."""
    with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
         patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
        yield mock_in, mock_out


@pytest.fixture
def mock_memory():
    """Create mock memory (buffer) and store for ChatFlow tests."""
    memory = MagicMock()
    memory.get_messages_for_api.return_value = []
    memory.should_compress.return_value = False

    store = MagicMock()
    store.search.return_value = []
    store.add_from_conversation = AsyncMock()

    return memory, store
