"""
Serializer for Memory objects.

This module provides a unified serialization interface for different memory types,
handling metadata flattening, version identification, and compatibility with
ChromaDB storage requirements.
"""

__all__ = [
    "MemorySerializer",
    "flatten_metadata",
]

import json
import logging
from typing import Dict, Any, Tuple, Union

from .memory_types import Memory
from .versioning import VersioningStrategyFactory

logger = logging.getLogger(__name__)


def flatten_metadata(metadata: Dict[str, Any]) -> Dict[str, Union[str, int, float, bool]]:
    """
    将元数据中的复杂类型（列表、字典）转换为 JSON 字符串，
    确保所有值都是 Chroma 支持的基本类型。

    Args:
        metadata: Dictionary with potentially complex values

    Returns:
        Dictionary with all values converted to basic types (str, int, float, bool)
    """
    flattened = {}
    for k, v in metadata.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            flattened[k] = v
        else:
            try:
                flattened[k] = json.dumps(v, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to serialize metadata field '{k}': {e}, skipping")
    return flattened


class MemorySerializer:
    """
    Unified serializer for Memory objects.

    Handles serialization of different memory types, integrates with versioning
    strategies, and ensures metadata compatibility with ChromaDB storage.
    """

    def __init__(self):
        """Initialize the serializer."""
        self.versioning_factory = VersioningStrategyFactory

    def serialize(self, memory: Memory) -> Tuple[str, Dict[str, Any]]:
        """
        Serialize a Memory object for storage in VectorMemory.

        Args:
            memory: The Memory object to serialize

        Returns:
            Tuple of (content_text, metadata_dict) where:
            - content_text: The main textual content to embed and store
            - metadata_dict: Flattened metadata ready for ChromaDB storage

        Raises:
            TypeError: If memory is not a valid Memory instance
            ValueError: If serialization fails
        """
        if not isinstance(memory, Memory):
            raise TypeError(f"Expected Memory instance, got {type(memory)}")

        try:
            # Get the appropriate versioning strategy for this memory type
            versioning_strategy = self.versioning_factory.get_strategy_for_memory(memory)

            # Generate base ID and version identifier fields
            base_id = versioning_strategy.generate_base_id(memory)
            version_identifier = versioning_strategy.get_version_identifier(memory)

            # Get memory's own content and metadata
            content_text = memory.get_content()
            memory_metadata = memory.get_metadata()

            # Combine all metadata
            combined_metadata = {
                "memory_type": memory.memory_type.value,
                "base_id": base_id,
                **version_identifier,
                **memory_metadata,
            }

            # Add backward compatibility fields
            if memory.memory_type.value == "fact":
                # For fact memories, also include base_fact_id for compatibility
                combined_metadata["base_fact_id"] = base_id

            # Remove None values to save space and avoid serialization issues
            combined_metadata = {k: v for k, v in combined_metadata.items() if v is not None}

            # Flatten metadata for ChromaDB compatibility
            flattened_metadata = flatten_metadata(combined_metadata)

            return content_text, flattened_metadata

        except Exception as e:
            logger.error(f"Failed to serialize memory of type {memory.memory_type}: {e}")
            raise ValueError(f"Serialization failed: {e}") from e

    @classmethod
    def serialize_memory(cls, memory: Memory) -> Tuple[str, Dict[str, Any]]:
        """
        Convenience class method for one-off serialization.

        Args:
            memory: The Memory object to serialize

        Returns:
            Tuple of (content_text, metadata_dict)
        """
        serializer = cls()
        return serializer.serialize(memory)