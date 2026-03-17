"""
Memory types for the VectorMemory system.

This module defines the abstract base class for all memory types,
concrete memory implementations (FactMemory, SummaryMemory),
and a registry for memory type registration.
"""

__all__ = [
    "Memory",
    "MemoryType",
    "FactMemory",
    "SummaryMemory",
    "MemoryRegistry",
]

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, List, Union, ClassVar, Type, TYPE_CHECKING
import json
import logging
from enum import Enum

if TYPE_CHECKING:
    from .memory_extractor import Fact

logger = logging.getLogger(__name__)


class MemoryType(str, Enum):
    """Enumeration of supported memory types."""
    FACT = "fact"
    SUMMARY = "summary"
    # Add more memory types as needed

    @classmethod
    def _missing_(cls, value):
        """
        Allow creation of MemoryType with arbitrary string values.
        This enables dynamic registration of new memory types.
        """
        # Create a new enum member dynamically
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj._name_ = value.upper().replace(".", "_").replace("-", "_")
        # Register with the enum's member map
        cls._member_map_[obj._name_] = obj
        cls._value2member_map_[value] = obj
        return obj


class Memory(ABC):
    """
    Abstract base class for all memory types.

    All memory types must implement this interface to be compatible
    with the VectorMemory storage and retrieval system.
    """

    @property
    @abstractmethod
    def memory_type(self) -> MemoryType:
        """Return the type of this memory."""
        pass

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert memory to a dictionary suitable for serialization.

        Returns:
            Dictionary representation of the memory.
        """
        pass

    @abstractmethod
    def get_content(self) -> str:
        """
        Get the main content text of the memory.

        Returns:
            The primary textual content to be embedded and stored.
        """
        pass

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        """
        Get metadata for this memory.

        Returns:
            Dictionary of metadata fields for storage.
        """
        pass

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Memory":
        """
        Optional class method to create a memory instance from a dictionary.

        Subclasses can override this method to provide custom deserialization.
        The default implementation raises NotImplementedError.

        Args:
            data: Dictionary representation of the memory

        Returns:
            Memory instance
        """
        _ = data  # Mark parameter as used for type checking
        raise NotImplementedError("Subclasses should implement from_dict if needed")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(type={self.memory_type}, content={self.get_content()[:50]}...)"


@dataclass
class FactMemory(Memory):
    """
    Memory wrapper for Fact objects.

    This class adapts the existing Fact class to the Memory interface,
    allowing Facts to be stored in VectorMemory alongside other memory types.
    """
    fact: "Fact"  # Forward reference, will import from memory_extractor

    @property
    def memory_type(self) -> MemoryType:
        return MemoryType.FACT

    def to_dict(self) -> Dict[str, Any]:
        """Convert the wrapped Fact to a dictionary."""
        return self.fact.to_dict()

    def get_content(self) -> str:
        """Return the fact text as the main content."""
        return self.fact.fact_text

    def get_metadata(self) -> Dict[str, Any]:
        """
        Extract metadata from the Fact object.

        Returns metadata compatible with VectorMemory's metadata schema.
        """
        from .memory_extractor import Fact  # Import here to avoid circular dependency

        if not isinstance(self.fact, Fact):
            raise TypeError(f"Expected Fact object, got {type(self.fact)}")

        # Extract core fields from Fact
        metadata = {
            "type": self.fact.type,
            "speaker": self.fact.speaker,
            "attribute": self.fact.attribute,
            "confidence": self.fact.confidence,
            "source": self.fact.source,
            "original_utterance": self.fact.original_utterance,
            "is_plausible": self.fact.is_plausible,
            "timestamp": self.fact.timestamp,
            "version": self.fact.version,
            "is_active": self.fact.is_active,
            "extracted_fact_id": self.fact.fact_id,
            "memory_type": self.memory_type.value,
        }

        # Add nested metadata from Fact.metadata
        if self.fact.metadata:
            metadata.update(self.fact.metadata)

        # Remove None values
        metadata = {k: v for k, v in metadata.items() if v is not None}

        return metadata

    def __repr__(self) -> str:
        """Return a string representation of the FactMemory."""
        # Use the parent class implementation for consistency
        return super().__repr__()

    @classmethod
    def from_fact(cls, fact: "Fact") -> "FactMemory":
        """Create a FactMemory from a Fact object."""
        return cls(fact=fact)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FactMemory":
        """Create a FactMemory from a dictionary representation."""
        from .memory_extractor import Fact

        # Provide defaults for missing required fields
        # Fact class constructor requires these fields, but we can provide
        # reasonable defaults for backward compatibility
        defaults = {
            "confidence": 0.8,  # Medium confidence
            "source": "memory_system",  # Default source
            "original_utterance": data.get("fact_text", ""),  # Use fact_text as fallback
            "is_plausible": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": 1,
            "is_active": True,
            "metadata": {},
            "type": "unknown",
            "speaker": "unknown",
            "attribute": "unknown"
        }

        # Merge defaults with provided data (provided data takes precedence)
        merged_data = {**defaults, **data}

        # Still require minimum fields
        min_required = ["fact_text", "type", "speaker", "attribute"]
        missing = [field for field in min_required if field not in merged_data]
        if missing:
            raise ValueError(f"Missing required fields for Fact: {missing}")

        # Create Fact instance
        fact = Fact(
            fact_text=merged_data["fact_text"],
            confidence=merged_data["confidence"],
            type=merged_data["type"],
            speaker=merged_data["speaker"],
            source=merged_data["source"],
            original_utterance=merged_data["original_utterance"],
            attribute=merged_data["attribute"],
            is_plausible=merged_data["is_plausible"],
            timestamp=merged_data["timestamp"],
            version=merged_data["version"],
            is_active=merged_data["is_active"],
            metadata=merged_data["metadata"]
        )

        # Set fact_id if present (preserve existing)
        if "fact_id" in merged_data:
            fact.fact_id = merged_data["fact_id"]

        return cls(fact=fact)


@dataclass
class SummaryMemory(Memory):
    """
    Memory type for conversation summaries.

    Stores enhanced summary information with key points and metadata.
    """
    summary_text: str
    conversation_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    key_points: List[str] = field(default_factory=list)
    length: int = 0  # Length in characters or tokens
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def memory_type(self) -> MemoryType:
        return MemoryType.SUMMARY

    def to_dict(self) -> Dict[str, Any]:
        """Convert SummaryMemory to dictionary."""
        return {
            "summary_text": self.summary_text,
            "conversation_id": self.conversation_id,
            "timestamp": self.timestamp,
            "key_points": self.key_points,
            "length": self.length,
            "metadata": self.metadata,
            "memory_type": self.memory_type.value,
            "confidence": 1.0,  # Summary memories have fixed high confidence
        }

    def get_content(self) -> str:
        """Return the summary text as main content."""
        return self.summary_text

    def get_metadata(self) -> Dict[str, Any]:
        """Get metadata for storage."""
        metadata = {
            "type": "conversation_summary",
            "conversation_id": self.conversation_id,
            "timestamp": self.timestamp,
            "key_points": json.dumps(self.key_points, ensure_ascii=False),
            "length": self.length,
            "memory_type": self.memory_type.value,
            "confidence": 1.0,  # Summary memories have fixed high confidence
        }

        # Add any additional metadata
        if self.metadata:
            metadata.update(self.metadata)

        # Remove None values
        metadata = {k: v for k, v in metadata.items() if v is not None}

        return metadata

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SummaryMemory":
        """Create a SummaryMemory from a dictionary."""
        # Handle key_points which might be stored as JSON string
        key_points = data.get("key_points", [])
        if isinstance(key_points, str):
            try:
                key_points = json.loads(key_points)
            except json.JSONDecodeError:
                key_points = []

        metadata = dict(data.get("metadata", {}))
        known_fields = {
            "id",
            "summary_text",
            "conversation_id",
            "timestamp",
            "key_points",
            "length",
            "metadata",
            "memory_type",
            "confidence",
        }
        for key, value in data.items():
            if key not in known_fields:
                metadata[key] = value

        return cls(
            summary_text=data["summary_text"],
            conversation_id=data["conversation_id"],
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            key_points=key_points,
            length=data.get("length", 0),
            metadata=metadata,
        )
    def add_key_point(self, point: str):
        """Add a key point to the summary."""
        self.key_points.append(point)


class MemoryRegistry:
    """
    Registry for memory types.

    Provides factory methods for creating memory instances and
    supports registering new memory types.
    """

    # Registry mapping memory type strings to class implementations
    _registry: ClassVar[Dict[str, Type[Memory]]] = {}

    @classmethod
    def register(cls, memory_type: Union[str, MemoryType], memory_class: Type[Memory]):
        """
        Register a memory type with its implementation class.

        Args:
            memory_type: String or MemoryType enum value
            memory_class: Class implementing the Memory interface
        """
        if isinstance(memory_type, MemoryType):
            memory_type = memory_type.value

        if not issubclass(memory_class, Memory):
            raise TypeError(f"Memory class must inherit from Memory, got {memory_class}")

        cls._registry[memory_type] = memory_class
        logger.debug(f"Registered memory type '{memory_type}' -> {memory_class.__name__}")

    @classmethod
    def create(cls, memory_type: Union[str, MemoryType], **kwargs) -> Memory:
        """
        Create a memory instance of the specified type.

        Args:
            memory_type: String or MemoryType enum value
            **kwargs: Arguments to pass to the memory class constructor

        Returns:
            Instance of the registered memory class
        """
        if isinstance(memory_type, MemoryType):
            memory_type = memory_type.value

        if memory_type not in cls._registry:
            raise ValueError(f"Unknown memory type: {memory_type}. "
                           f"Registered types: {list(cls._registry.keys())}")

        memory_class = cls._registry[memory_type]
        return memory_class(**kwargs)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Memory:
        """
        Create a memory instance from a dictionary representation.

        The dictionary must contain a 'memory_type' field indicating
        which memory type to instantiate.

        Args:
            data: Dictionary with memory data

        Returns:
            Memory instance
        """
        memory_type = data.get("memory_type")
        if not memory_type:
            # Try to infer type from other fields
            if "fact_text" in data:
                memory_type = MemoryType.FACT.value
            elif "summary_text" in data:
                memory_type = MemoryType.SUMMARY.value
            else:
                raise ValueError("Cannot determine memory type from dictionary")

        if memory_type not in cls._registry:
            raise ValueError(f"Unknown memory type: {memory_type}")

        memory_class = cls._registry[memory_type]

        # Use from_dict method if available
        if hasattr(memory_class, 'from_dict'):
            # mypy/type checkers might not know memory_class has from_dict
            # Use cast to help the type checker
            from_dict_method = memory_class.from_dict  # type: ignore[attr-defined]
            return from_dict_method(data)

        # Otherwise, try to construct directly
        return memory_class(**data)

    @classmethod
    def get_registered_types(cls) -> List[str]:
        """Get list of registered memory type strings."""
        return list(cls._registry.keys())

    @classmethod
    def clear_registry(cls):
        """Clear the registry (mainly for testing)."""
        cls._registry.clear()


# Auto-register built-in memory types
MemoryRegistry.register(MemoryType.FACT, FactMemory)
MemoryRegistry.register(MemoryType.SUMMARY, SummaryMemory)