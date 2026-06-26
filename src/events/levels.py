"""EventLevel — 事件展示级别。"""

from enum import IntEnum

class EventLevel(IntEnum):
    """三级事件粒度。

    PROGRESS: token_delta、错误（默认）
    DETAIL: thinking_delta（思考正文）、agent_state_changed
    TRACE:
    """

    PROGRESS = 1
    DETAIL = 2
    TRACE = 3

    @classmethod
    def from_str(cls, value: str) -> "EventLevel":
        """从字符串解析级别，无效值返回 PROGRESS。"""
        try:
            return cls[value.upper()]
        except KeyError:
            return cls.PROGRESS
