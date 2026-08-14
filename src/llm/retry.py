"""LLM 自动重试配置与统一退避计算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
from typing import Callable

from src.llm.errors import LLMErrorInfo


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """LLM 自动重试次数与退避范围配置。"""

    max_attempts: int = 10
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        """校验尝试次数与退避范围。

        Returns:
            None。

        Raises:
            ValueError: 次数或延迟的类型、有限性及范围非法。
        """
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts 必须是非 bool 且大于等于 1 的整数")

        for key, value in (
            ("base_delay_seconds", self.base_delay_seconds),
            ("max_delay_seconds", self.max_delay_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{key} 必须是非 bool 的有限正数")

        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 必须大于等于 base_delay_seconds")


class RetryPolicy:
    """基于响应头和指数退避计算下一次等待时间。"""

    def __init__(
        self,
        config: RetryConfig | None = None,
        *,
        random_value: Callable[[], float] | None = None,
        now: Callable[[], datetime | float] | None = None,
    ) -> None:
        """初始化重试策略。

        Args:
            config: 重试次数与延迟配置；缺省时使用默认值。
            random_value: 返回零到一随机值的函数，供测试确定抖动。
            now: 返回当前 UTC 时间或 Unix 时间戳的函数，供解析 HTTP date。

        Returns:
            None。
        """
        self.config = config or RetryConfig()
        self._random_value = random_value or random.random
        self._now = now or (lambda: datetime.now(timezone.utc))

    def should_retry(
        self,
        info: LLMErrorInfo,
        attempt: int,
        *,
        max_attempts: int | None = None,
    ) -> bool:
        """判断当前失败后是否仍可自动重试。

        Args:
            info: 已分类错误信息。
            attempt: 已失败的 1 基尝试序号。
            max_attempts: 本次调用的最大尝试次数；None 时使用策略配置。

        Returns:
            错误可重试且尚未耗尽最大尝试次数时为 True。

        Raises:
            ValueError: max_attempts 不是非 bool 正整数。
        """
        limit = self.config.max_attempts if max_attempts is None else max_attempts
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("max_attempts 必须是非 bool 且大于等于 1 的整数")
        return info.retryable and attempt < limit

    def delay(self, info: LLMErrorInfo, *, attempt: int) -> float:
        """按响应头优先级计算下一次尝试前的等待秒数。

        Args:
            info: 已分类错误信息及 Retry-After 元数据。
            attempt: 已失败的 1 基尝试序号。

        Returns:
            经过最大延迟封顶的非负等待秒数。
        """
        return calculate_retry_delay(
            info,
            attempt=attempt,
            config=self.config,
            random_value=self._random_value,
            now=self._now,
        )


def calculate_retry_delay(
    info: LLMErrorInfo,
    *,
    attempt: int,
    config: RetryConfig | None = None,
    random_value: Callable[[], float] | None = None,
    now: Callable[[], datetime | float] | None = None,
) -> float:
    """按毫秒响应头、Retry-After、指数退避顺序计算等待时间。

    Args:
        info: 已分类错误信息及 Retry-After 元数据。
        attempt: 已失败的 1 基尝试序号。
        config: 重试次数与延迟配置；缺省时使用默认值。
        random_value: 返回零到一随机值的函数，供测试确定抖动。
        now: 返回当前 UTC 时间或 Unix 时间戳的函数，供解析 HTTP date。

    Returns:
        经过最大延迟封顶的非负等待秒数。

    Raises:
        ValueError: attempt 小于一。
    """
    if attempt < 1:
        raise ValueError("attempt 必须大于等于 1")
    retry_config = config or RetryConfig()

    if (
        info.retry_after_ms is not None
        and math.isfinite(info.retry_after_ms)
        and info.retry_after_ms >= 0
    ):
        delay = info.retry_after_ms / 1000.0
    else:
        delay = _retry_after_seconds(info.retry_after, now=now)
        if delay is None:
            random_source = random_value or random.random
            jitter_source = min(max(float(random_source()), 0.0), 1.0)
            jitter = 0.75 + 0.25 * jitter_source
            delay = retry_config.base_delay_seconds * 2 ** (attempt - 1) * jitter

    return min(max(delay, 0.0), retry_config.max_delay_seconds)


def _retry_after_seconds(
    value: str | None,
    *,
    now: Callable[[], datetime | float] | None,
) -> float | None:
    """把 Retry-After 秒数或 HTTP date 转换为等待秒数。

    Args:
        value: Retry-After 原始响应头。
        now: 返回当前 UTC 时间或 Unix 时间戳的函数。

    Returns:
        非负等待秒数；响应头缺失或非法时为 None。
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None and math.isfinite(seconds):
        return max(seconds, 0.0)

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current_value = now() if now is not None else datetime.now(timezone.utc)
    if isinstance(current_value, (int, float)):
        current = datetime.fromtimestamp(float(current_value), tz=timezone.utc)
    else:
        current = current_value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    return max((target - current).total_seconds(), 0.0)
