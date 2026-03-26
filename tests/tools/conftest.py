"""Tools module test fixtures."""
import pytest
from src.tools.registry import ToolRegistry


@pytest.fixture
def clean_registry():
    """Provide a fresh ToolRegistry for each test."""
    return ToolRegistry()
