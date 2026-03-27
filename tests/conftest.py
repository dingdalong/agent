"""Root-level shared pytest fixtures."""
import os
import shutil
import pytest
from unittest.mock import AsyncMock

from src.llm.types import LLMResponse

WORKSPACE = os.path.abspath("./workspace")


@pytest.fixture
def workspace_dir():
    """Create and clean up the workspace directory for file tool tests."""
    os.makedirs(WORKSPACE, exist_ok=True)
    yield WORKSPACE
    for name in os.listdir(WORKSPACE):
        if name.startswith("test_"):
            path = os.path.join(WORKSPACE, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)


@pytest.fixture
def mock_llm():
    """Provide a mock LLM for tests."""
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(content="test response", tool_calls={}, finish_reason="stop")
    return llm
