"""
Unit tests for the memory decay system (decay.py).
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

from src.memory.decay import (
    FREQUENCY_CAP,
    RECENCY_LAMBDA,
    calculate_importance,
)
from src.memory.types import MemoryRecord, MemoryType


class TestCalculateImportance(unittest.TestCase):

    def _make_fact(self, **overrides) -> MemoryRecord:
        defaults = dict(
            memory_type=MemoryType.FACT,
            content="test",
            confidence=1.0,
            access_count=0,
        )
        defaults.update(overrides)
        return MemoryRecord(**defaults)

    def test_summary_always_returns_one(self):
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content="summary",
            confidence=0.1,
            access_count=0,
        )
        now = datetime.now(timezone.utc)
        self.assertEqual(calculate_importance(record, now), 1.0)

    def test_brand_new_zero_access(self):
        """A brand-new fact with 0 access should have 0 frequency weight -> importance = 0."""
        now = datetime.now(timezone.utc)
        record = self._make_fact(last_accessed=now, access_count=0, confidence=1.0)
        importance = calculate_importance(record, now)
        # log(0+1)/log(20) = 0 → importance = 0
        self.assertAlmostEqual(importance, 0.0, places=4)

    def test_one_access_produces_nonzero(self):
        now = datetime.now(timezone.utc)
        record = self._make_fact(last_accessed=now, access_count=1, confidence=1.0)
        importance = calculate_importance(record, now)
        expected_freq = math.log(2) / math.log(FREQUENCY_CAP)
        self.assertAlmostEqual(importance, round(expected_freq, 4), places=4)

    def test_max_frequency_cap(self):
        """At access_count = FREQUENCY_CAP - 1, frequency weight should be ~1.0."""
        now = datetime.now(timezone.utc)
        record = self._make_fact(
            last_accessed=now,
            access_count=FREQUENCY_CAP - 1,
            confidence=1.0,
        )
        importance = calculate_importance(record, now)
        # frequency_w = min(1.0, log(20)/log(20)) = 1.0, recency_w ≈ 1.0
        self.assertAlmostEqual(importance, 1.0, places=2)

    def test_recency_decay_over_time(self):
        """Importance should decrease as days pass."""
        now = datetime.now(timezone.utc)
        record_recent = self._make_fact(
            last_accessed=now, access_count=5, confidence=0.9,
        )
        record_old = self._make_fact(
            last_accessed=now - timedelta(days=70),
            access_count=5,
            confidence=0.9,
        )
        imp_recent = calculate_importance(record_recent, now)
        imp_old = calculate_importance(record_old, now)
        self.assertGreater(imp_recent, imp_old)

    def test_half_life_approximately_70_days(self):
        """At ~70 days, recency weight should be about 0.5."""
        now = datetime.now(timezone.utc)
        half_life_days = math.log(2) / RECENCY_LAMBDA
        record = self._make_fact(
            last_accessed=now - timedelta(days=half_life_days),
            access_count=FREQUENCY_CAP,  # max frequency so it doesn't affect test
            confidence=1.0,
        )
        importance = calculate_importance(record, now)
        # Should be approximately 0.5
        self.assertAlmostEqual(importance, 0.5, places=1)

    def test_confidence_scales_linearly(self):
        now = datetime.now(timezone.utc)
        record_high = self._make_fact(
            last_accessed=now, access_count=10, confidence=1.0,
        )
        record_low = self._make_fact(
            last_accessed=now, access_count=10, confidence=0.5,
        )
        imp_high = calculate_importance(record_high, now)
        imp_low = calculate_importance(record_low, now)
        self.assertAlmostEqual(imp_low / imp_high, 0.5, places=2)

    def test_custom_lambda(self):
        """Custom recency_lambda should change decay rate."""
        now = datetime.now(timezone.utc)
        record = self._make_fact(
            last_accessed=now - timedelta(days=10),
            access_count=10,
            confidence=1.0,
        )
        imp_default = calculate_importance(record, now)
        imp_fast = calculate_importance(record, now, recency_lambda=0.1)
        # Faster decay → lower importance
        self.assertGreater(imp_default, imp_fast)

    def test_now_defaults_to_utc(self):
        """If now is not provided, should use current UTC time."""
        record = self._make_fact(
            last_accessed=datetime.now(timezone.utc),
            access_count=5,
            confidence=0.9,
        )
        importance = calculate_importance(record)
        self.assertGreater(importance, 0.0)

    def test_returns_rounded_value(self):
        now = datetime.now(timezone.utc)
        record = self._make_fact(
            last_accessed=now - timedelta(days=3),
            access_count=7,
            confidence=0.85,
        )
        importance = calculate_importance(record, now)
        # Should be rounded to 4 decimal places
        self.assertEqual(importance, round(importance, 4))


if __name__ == "__main__":
    unittest.main()
