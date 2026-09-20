"""项目记忆的类型词汇 — MemoryMgr 与记忆工具共用的单一权威。"""

from __future__ import annotations

from typing import Literal, get_args

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES = get_args(MemoryType)
