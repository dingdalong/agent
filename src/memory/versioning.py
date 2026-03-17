"""
Versioning strategies for different memory types.

This module provides the abstract base class for version control strategies,
concrete implementations for each memory type, and a factory for obtaining
the appropriate strategy for a given memory type.
"""

__all__ = [
    "VersioningStrategy",
    "FactVersioningStrategy",
    "SummaryVersioningStrategy",
    "VersioningStrategyFactory",
    "MemoryType",
]

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from .memory_types import Memory, MemoryType

logger = logging.getLogger(__name__)


class VersioningStrategy(ABC):
    """
    Abstract base class for version control strategies.

    Each memory type may have different requirements for generating base IDs
    and version identifiers. This interface defines the common operations.
    """

    @abstractmethod
    def generate_base_id(self, memory: Memory) -> str:
        """
        Generate a unique base ID for a memory, excluding version information.

        The base ID is used to group different versions of the same logical memory.
        It should be deterministic based on the memory's identity fields.

        Args:
            memory: The memory object

        Returns:
            String representation of the base ID (hex digest)
        """
        pass

    @abstractmethod
    def get_version_identifier(self, memory: Memory) -> Dict[str, Any]:
        """
        Extract the fields that uniquely identify this version of the memory.

        These fields are typically stored in metadata and used for version
        tracking and conflict resolution.

        Args:
            memory: The memory object

        Returns:
            Dictionary of identifying fields
        """
        pass


class FactVersioningStrategy(VersioningStrategy):
    """
    Versioning strategy for Fact memories.

    Generates base ID from speaker, type, and attribute fields (matching the
    existing VectorMemory._generate_base_id logic for backward compatibility).
    """

    def generate_base_id(self, memory: Memory) -> str:
        """
        Generate base ID using hash of "speaker|type|attribute".

        Args:
            memory: Must be a FactMemory instance

        Returns:
            SHA256 hex digest of the concatenated identity fields

        Raises:
            TypeError: If memory is not a FactMemory
        """
        from .memory_types import FactMemory

        if not isinstance(memory, FactMemory):
            raise TypeError(f"Expected FactMemory, got {type(memory)}")

        # Extract identity fields from the wrapped Fact object
        fact = memory.fact
        speaker = fact.speaker
        fact_type = fact.type
        attribute = fact.attribute

        # Concatenate with pipe separator (matching existing implementation)
        base = f"{speaker}|{fact_type}|{attribute}"
        return hashlib.sha256(base.encode()).hexdigest()

    def get_version_identifier(self, memory: Memory) -> Dict[str, Any]:
        """
        Extract speaker, type, and attribute fields for version identification.

        Args:
            memory: Must be a FactMemory instance

        Returns:
            Dictionary with keys: speaker, type, attribute

        Raises:
            TypeError: If memory is not a FactMemory
        """
        from .memory_types import FactMemory

        if not isinstance(memory, FactMemory):
            raise TypeError(f"Expected FactMemory, got {type(memory)}")

        fact = memory.fact
        return {
            "speaker": fact.speaker,
            "type": fact.type,
            "attribute": fact.attribute,
        }


class SummaryVersioningStrategy(VersioningStrategy):
    """
    Versioning strategy for Summary memories.

    Generates base ID from conversation_id field.
    """

    def generate_base_id(self, memory: Memory) -> str:
        """
        Generate base ID using hash of "conversation_id".

        Args:
            memory: Must be a SummaryMemory instance

        Returns:
            SHA256 hex digest of the conversation_id

        Raises:
            TypeError: If memory is not a SummaryMemory
        """
        from .memory_types import SummaryMemory

        if not isinstance(memory, SummaryMemory):
            raise TypeError(f"Expected SummaryMemory, got {type(memory)}")

        # Extract conversation_id
        conversation_id = memory.conversation_id
        base = f"{conversation_id}"
        return hashlib.sha256(base.encode()).hexdigest()

    def get_version_identifier(self, memory: Memory) -> Dict[str, Any]:
        """
        Extract conversation_id for version identification.

        Args:
            memory: Must be a SummaryMemory instance

        Returns:
            Dictionary with key: conversation_id

        Raises:
            TypeError: If memory is not a SummaryMemory
        """
        from .memory_types import SummaryMemory

        if not isinstance(memory, SummaryMemory):
            raise TypeError(f"Expected SummaryMemory, got {type(memory)}")

        return {
            "conversation_id": memory.conversation_id,
        }


class VersioningStrategyFactory:
    """
    Factory for obtaining versioning strategies based on memory type.

    Maps MemoryType enum values to appropriate strategy instances.
    This factory uses a singleton pattern for each strategy type to ensure
    thread-safe reuse of stateless strategy objects.
    """

    # Singleton instances of each strategy (initialized lazily)
    _fact_strategy: Optional[FactVersioningStrategy] = None
    _summary_strategy: Optional[SummaryVersioningStrategy] = None

    # Registry mapping memory types to strategy classes
    _registry = {
        MemoryType.FACT: FactVersioningStrategy,
        MemoryType.SUMMARY: SummaryVersioningStrategy,
    }

    @classmethod
    def get_strategy(cls, memory_type: MemoryType) -> VersioningStrategy:
        """
        Get the appropriate versioning strategy for a memory type.

        Args:
            memory_type: The MemoryType enum value

        Returns:
            An instance of the appropriate VersioningStrategy subclass

        Raises:
            ValueError: If the memory type is not supported
        """
        if memory_type not in cls._registry:
            raise ValueError(f"Unsupported memory type: {memory_type}")

        strategy_class = cls._registry[memory_type]

        # Return singleton instance for the strategy class
        if memory_type == MemoryType.FACT:
            if cls._fact_strategy is None:
                cls._fact_strategy = strategy_class()
            # At this point, _fact_strategy is not None
            assert cls._fact_strategy is not None
            return cls._fact_strategy
        elif memory_type == MemoryType.SUMMARY:
            if cls._summary_strategy is None:
                cls._summary_strategy = strategy_class()
            # At this point, _summary_strategy is not None
            assert cls._summary_strategy is not None
            return cls._summary_strategy
        else:
            # For future memory types, create new instance each time
            # (can be optimized later if needed)
            return strategy_class()

    @classmethod
    def get_strategy_for_memory(cls, memory: Memory) -> VersioningStrategy:
        """
        Get the appropriate versioning strategy for a memory instance.

        Args:
            memory: The memory object

        Returns:
            An instance of the appropriate VersioningStrategy subclass
        """
        return cls.get_strategy(memory.memory_type)

    @classmethod
    def register_strategy(cls, memory_type: MemoryType, strategy_class):
        """
        Register a new memory type with its versioning strategy.

        This allows extending the system with custom memory types.

        Args:
            memory_type: MemoryType enum value
            strategy_class: Class implementing VersioningStrategy

        Raises:
            TypeError: If strategy_class does not inherit from VersioningStrategy
        """
        if not issubclass(strategy_class, VersioningStrategy):
            raise TypeError(f"Strategy class must inherit from VersioningStrategy, got {strategy_class}")

        cls._registry[memory_type] = strategy_class
        logger.debug(f"Registered versioning strategy for memory type '{memory_type.value}' -> {strategy_class.__name__}")