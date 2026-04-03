import time
import functools
import logging
import asyncio

logger = logging.getLogger(__name__)

def time_function(log_threshold=1.0):
    """
    性能监控装饰器，记录函数执行时间。

    Args:
        log_threshold: 超过此阈值（秒）时记录为 WARNING 级别
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start

            if elapsed > log_threshold:
                logger.warning(f"SLOW: {func.__name__} took {elapsed:.2f}s")
            else:
                logger.debug(f"TIMING: {func.__name__} took {elapsed:.3f}s")

            return result
        return wrapper
    return decorator


def async_time_function(log_threshold=1.0):
    """异步函数计时装饰器。

    Args:
        log_threshold: 超过此阈值（秒）时记录为 WARNING 级别
    """
    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                elapsed = time.perf_counter() - start
                if elapsed > log_threshold:
                    logger.warning(f"SLOW: {func.__name__} took {elapsed:.2f}s")
                else:
                    logger.debug(f"TIMING: {func.__name__} took {elapsed:.3f}s")
        return async_wrapper
    return decorator
