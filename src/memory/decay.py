"""记忆衰减系统。

基于置信度、访问时间和访问频率计算记忆重要性权重。
"""

import math
from datetime import datetime, timezone

from .types import MemoryRecord, MemoryType

# 衰减参数
RECENCY_LAMBDA = 0.01  # 半衰期约 70 天
FREQUENCY_CAP = 20  # access_count 达到此值后权重不再增长


def calculate_importance(
    record: MemoryRecord,
    now: datetime | None = None,
    recency_lambda: float = RECENCY_LAMBDA,
    frequency_cap: int = FREQUENCY_CAP,
) -> float:
    """计算记忆的重要性权重。

    importance = confidence_weight * recency_weight * frequency_weight

    - confidence_weight: record.confidence (0~1)
    - recency_weight: exp(-lambda * days_since_last_access)
    - frequency_weight: min(1.0, log(access_count + 1) / log(frequency_cap))

    Summary 类型不衰减，始终返回 1.0。
    """
    if record.memory_type == MemoryType.SUMMARY:
        return 1.0

    now = now or datetime.now(timezone.utc)
    days = max(0.0, (now - record.last_accessed).total_seconds() / 86400)

    confidence_w = record.confidence
    recency_w = math.exp(-recency_lambda * days)
    frequency_w = min(1.0, math.log(record.access_count + 1) / math.log(frequency_cap))

    return round(confidence_w * recency_w * frequency_w, 4)
