"""Root-level shared pytest fixtures."""
import os
import shutil
import pytest
from unittest.mock import AsyncMock, patch

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
def mock_call_model():
    """Patch call_model and return the mock for configuration."""
    with patch("src.core.async_api.call_model", new_callable=AsyncMock) as mock:
        yield mock
