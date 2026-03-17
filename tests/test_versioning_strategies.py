"""
Unit tests for versioning strategies.

Tests VersioningStrategy, FactVersioningStrategy, SummaryVersioningStrategy,
and VersioningStrategyFactory.
"""

import unittest
import hashlib
from unittest.mock import Mock, patch

# Import the modules to test
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.memory.versioning import (
    VersioningStrategy,
    FactVersioningStrategy,
    SummaryVersioningStrategy,
    VersioningStrategyFactory,
    MemoryType
)
from src.memory.memory_types import FactMemory, SummaryMemory
from src.memory.memory_extractor import Fact


class TestVersioningStrategies(unittest.TestCase):
    """Test versioning strategies for different memory types."""

    def setUp(self):
        """Set up test fixtures."""
        pass

    def test_versioning_strategy_abstract(self):
        """Test that VersioningStrategy is an abstract base class."""
        with self.assertRaises(TypeError):
            # Cannot instantiate abstract class
            VersioningStrategy()

    def test_fact_versioning_strategy_generate_base_id(self):
        """Test FactVersioningStrategy.generate_base_id."""
        strategy = FactVersioningStrategy()

        # Create a mock Fact and FactMemory
        mock_fact = Mock(spec=Fact)
        mock_fact.speaker = "user"
        mock_fact.type = "user.preference"
        mock_fact.attribute = "user.preference.drink.coffee"

        mock_memory = Mock(spec=FactMemory)
        mock_memory.fact = mock_fact

        # Generate base ID
        base_id = strategy.generate_base_id(mock_memory)

        # Verify it matches the expected hash
        expected_base = "user|user.preference|user.preference.drink.coffee"
        expected_hash = hashlib.sha256(expected_base.encode()).hexdigest()

        self.assertEqual(base_id, expected_hash)

    def test_fact_versioning_strategy_wrong_memory_type(self):
        """Test FactVersioningStrategy with wrong memory type."""
        strategy = FactVersioningStrategy()

        # Pass a SummaryMemory to FactVersioningStrategy (should raise TypeError)
        mock_summary_memory = Mock(spec=SummaryMemory)

        with self.assertRaises(TypeError):
            strategy.generate_base_id(mock_summary_memory)

        with self.assertRaises(TypeError):
            strategy.get_version_identifier(mock_summary_memory)

    def test_fact_versioning_strategy_get_version_identifier(self):
        """Test FactVersioningStrategy.get_version_identifier."""
        strategy = FactVersioningStrategy()

        # Create a mock Fact and FactMemory
        mock_fact = Mock(spec=Fact)
        mock_fact.speaker = "assistant"
        mock_fact.type = "world.fact"
        mock_fact.attribute = "world.fact.capital.france.paris"

        mock_memory = Mock(spec=FactMemory)
        mock_memory.fact = mock_fact

        # Get version identifier
        identifier = strategy.get_version_identifier(mock_memory)

        # Verify fields
        self.assertEqual(identifier["speaker"], "assistant")
        self.assertEqual(identifier["type"], "world.fact")
        self.assertEqual(identifier["attribute"], "world.fact.capital.france.paris")
        self.assertEqual(len(identifier), 3)  # Only speaker, type, attribute

    def test_summary_versioning_strategy_generate_base_id(self):
        """Test SummaryVersioningStrategy.generate_base_id."""
        strategy = SummaryVersioningStrategy()

        # Create a mock SummaryMemory
        mock_memory = Mock(spec=SummaryMemory)
        mock_memory.conversation_id = "conv_abc123"

        # Generate base ID
        base_id = strategy.generate_base_id(mock_memory)

        # Verify it matches the expected hash
        expected_base = "conv_abc123"
        expected_hash = hashlib.sha256(expected_base.encode()).hexdigest()

        self.assertEqual(base_id, expected_hash)

    def test_summary_versioning_strategy_wrong_memory_type(self):
        """Test SummaryVersioningStrategy with wrong memory type."""
        strategy = SummaryVersioningStrategy()

        # Pass a FactMemory to SummaryVersioningStrategy (should raise TypeError)
        mock_fact_memory = Mock(spec=FactMemory)

        with self.assertRaises(TypeError):
            strategy.generate_base_id(mock_fact_memory)

        with self.assertRaises(TypeError):
            strategy.get_version_identifier(mock_fact_memory)

    def test_summary_versioning_strategy_get_version_identifier(self):
        """Test SummaryVersioningStrategy.get_version_identifier."""
        strategy = SummaryVersioningStrategy()

        # Create a mock SummaryMemory
        mock_memory = Mock(spec=SummaryMemory)
        mock_memory.conversation_id = "session_789xyz"

        # Get version identifier
        identifier = strategy.get_version_identifier(mock_memory)

        # Verify fields
        self.assertEqual(identifier["conversation_id"], "session_789xyz")
        self.assertEqual(len(identifier), 1)  # Only conversation_id

    def test_versioning_strategy_factory_get_strategy(self):
        """Test VersioningStrategyFactory.get_strategy."""
        # Get strategies for each memory type
        fact_strategy = VersioningStrategyFactory.get_strategy(MemoryType.FACT)
        summary_strategy = VersioningStrategyFactory.get_strategy(MemoryType.SUMMARY)

        # Verify correct types
        self.assertIsInstance(fact_strategy, FactVersioningStrategy)
        self.assertIsInstance(summary_strategy, SummaryVersioningStrategy)

        # Verify singleton pattern (same instance returned)
        fact_strategy2 = VersioningStrategyFactory.get_strategy(MemoryType.FACT)
        self.assertIs(fact_strategy, fact_strategy2)

        summary_strategy2 = VersioningStrategyFactory.get_strategy(MemoryType.SUMMARY)
        self.assertIs(summary_strategy, summary_strategy2)

    def test_versioning_strategy_factory_get_strategy_for_memory(self):
        """Test VersioningStrategyFactory.get_strategy_for_memory."""
        # Create mock memories with memory_type property
        mock_fact_memory = Mock(spec=FactMemory)
        mock_fact_memory.memory_type = MemoryType.FACT

        mock_summary_memory = Mock(spec=SummaryMemory)
        mock_summary_memory.memory_type = MemoryType.SUMMARY

        # Get strategies
        fact_strategy = VersioningStrategyFactory.get_strategy_for_memory(mock_fact_memory)
        summary_strategy = VersioningStrategyFactory.get_strategy_for_memory(mock_summary_memory)

        # Verify correct types
        self.assertIsInstance(fact_strategy, FactVersioningStrategy)
        self.assertIsInstance(summary_strategy, SummaryVersioningStrategy)

    def test_versioning_strategy_factory_unsupported_memory_type(self):
        """Test VersioningStrategyFactory with unsupported memory type."""
        # Try to get strategy for unknown memory type
        with self.assertRaises(ValueError):
            VersioningStrategyFactory.get_strategy(MemoryType("unknown"))

    def test_versioning_strategy_factory_register_strategy(self):
        """Test registering a new strategy."""
        # Define a custom strategy
        class CustomVersioningStrategy(VersioningStrategy):
            def generate_base_id(self, memory):
                return "custom_base_id"

            def get_version_identifier(self, memory):
                return {"custom": "identifier"}

        # Register for a new memory type
        VersioningStrategyFactory.register_strategy(
            MemoryType("custom"),
            CustomVersioningStrategy
        )

        # Get the strategy
        strategy = VersioningStrategyFactory.get_strategy(MemoryType("custom"))
        self.assertIsInstance(strategy, CustomVersioningStrategy)

        # Verify it works
        mock_memory = Mock()
        self.assertEqual(strategy.generate_base_id(mock_memory), "custom_base_id")
        self.assertEqual(strategy.get_version_identifier(mock_memory), {"custom": "identifier"})

    def test_versioning_strategy_factory_register_invalid_strategy(self):
        """Test registering a strategy that doesn't inherit from VersioningStrategy."""
        # Define a class that doesn't inherit from VersioningStrategy
        class InvalidStrategy:
            pass

        # Should raise TypeError (using valid MemoryType to test strategy validation)
        with self.assertRaises(TypeError):
            VersioningStrategyFactory.register_strategy(
                MemoryType.FACT,  # Valid memory type, invalid strategy class
                InvalidStrategy
            )

    def test_consistent_hashing(self):
        """Test that hashing is consistent for same inputs."""
        strategy = FactVersioningStrategy()

        # Create two identical FactMemory objects
        mock_fact1 = Mock(spec=Fact)
        mock_fact1.speaker = "user"
        mock_fact1.type = "user.name"
        mock_fact1.attribute = "user.name.first"

        mock_fact2 = Mock(spec=Fact)
        mock_fact2.speaker = "user"
        mock_fact2.type = "user.name"
        mock_fact2.attribute = "user.name.first"

        mock_memory1 = Mock(spec=FactMemory)
        mock_memory1.fact = mock_fact1

        mock_memory2 = Mock(spec=FactMemory)
        mock_memory2.fact = mock_fact2

        # Generate base IDs
        base_id1 = strategy.generate_base_id(mock_memory1)
        base_id2 = strategy.generate_base_id(mock_memory2)

        # They should be identical
        self.assertEqual(base_id1, base_id2)

        # Different inputs should produce different hashes
        mock_fact3 = Mock(spec=Fact)
        mock_fact3.speaker = "user"
        mock_fact3.type = "user.name"
        mock_fact3.attribute = "user.name.last"  # Different attribute

        mock_memory3 = Mock(spec=FactMemory)
        mock_memory3.fact = mock_fact3

        base_id3 = strategy.generate_base_id(mock_memory3)
        self.assertNotEqual(base_id1, base_id3)

    def test_version_identifier_dict_structure(self):
        """Test that version identifiers have correct dictionary structure."""
        # Test FactVersioningStrategy
        fact_strategy = FactVersioningStrategy()

        mock_fact = Mock(spec=Fact)
        mock_fact.speaker = "assistant"
        mock_fact.type = "assistant.capability"
        mock_fact.attribute = "assistant.capability.math"

        mock_memory = Mock(spec=FactMemory)
        mock_memory.fact = mock_fact

        fact_identifier = fact_strategy.get_version_identifier(mock_memory)
        self.assertIsInstance(fact_identifier, dict)
        self.assertEqual(set(fact_identifier.keys()), {"speaker", "type", "attribute"})

        # Test SummaryVersioningStrategy
        summary_strategy = SummaryVersioningStrategy()

        mock_summary = Mock(spec=SummaryMemory)
        mock_summary.conversation_id = "test_session"

        summary_identifier = summary_strategy.get_version_identifier(mock_summary)
        self.assertIsInstance(summary_identifier, dict)
        self.assertEqual(set(summary_identifier.keys()), {"conversation_id"})


if __name__ == '__main__':
    unittest.main()