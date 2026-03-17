"""
Memory system for AI Agent.

This package provides memory management components including:
- Short-term conversation buffer
- Long-term vector memory with version control
- Fact extraction from conversations
- Multi-type memory system (facts, summaries, etc.)
"""

__all__ = [
    # From memory.py
    "ConversationBuffer",
    "VectorMemory",
    "summarize_conversation",

    # From memory_extractor.py
    "Fact",
    "FactExtractor",
    "ExtractorConfig",

    # From memory_types.py
    "Memory",
    "MemoryType",
    "FactMemory",
    "SummaryMemory",
    "MemoryRegistry",

    # From versioning.py
    "VersioningStrategy",
    "FactVersioningStrategy",
    "SummaryVersioningStrategy",
    "VersioningStrategyFactory",

    # From serializer.py
    "MemorySerializer",
    "flatten_metadata",
]

# Re-export core components for backward compatibility and convenience

# From memory.py
from .memory import ConversationBuffer, VectorMemory, summarize_conversation

# From memory_extractor.py
from .memory_extractor import Fact, FactExtractor, ExtractorConfig

# From memory_types.py
from .memory_types import (
    Memory,
    MemoryType,
    FactMemory,
    SummaryMemory,
    MemoryRegistry,
)

# From versioning.py
from .versioning import (
    VersioningStrategy,
    FactVersioningStrategy,
    SummaryVersioningStrategy,
    VersioningStrategyFactory,
)

# From serializer.py
from .serializer import MemorySerializer, flatten_metadata

# Optional: Provide version info
__version__ = "0.1.0"
__author__ = "AI Agent Memory System"