# src/memory/chroma/utils.py
"""Memory 工具函数。"""

import re


def build_collection_name(prefix: str, user_id: str | None) -> str:
    """构建 ChromaDB collection 名称，基于前缀和用户 ID。"""
    if not user_id:
        return prefix
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized:
        return prefix
    return f"{prefix}_{sanitized}"[:63].strip("_")
