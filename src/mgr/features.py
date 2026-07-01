"""feature 合法名单与解析 — 角色可插拔 Manager 的开关集合。

角色在 role.md frontmatter 声明 features 列表，决定启用哪些可插拔 Manager
及其工具、提示词段。未声明（None）→ 全部启用（向后兼容）；声明 → 取与合法名单
的交集并校验依赖。模块零 Manager 依赖，供 bootstrap 与 Agent 共同引用。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 全部可插拔 feature 名 — 每个对应一个按需创建的 Manager 及其工具、提示词段。
ALL_FEATURES = frozenset({"task", "skill", "subagent", "file", "memory", "plan"})


def resolve_features(declared: set[str] | None) -> set[str]:
    """将角色声明的 feature 集解析为有效启用集。

    Args:
        declared: 角色声明的 feature 名集合；None 表示未声明。

    Returns:
        有效启用的 feature 名集合。None → 全部启用；否则取与 ALL_FEATURES 的交集
        （未知名告警丢弃），再校验依赖：plan 依赖 file，缺 file 时丢弃 plan 并告警。
    """
    if declared is None:
        return set(ALL_FEATURES)
    feats = set(declared) & set(ALL_FEATURES)
    unknown = set(declared) - set(ALL_FEATURES)
    if unknown:
        logger.warning(
            "未知 feature 已忽略：%s；合法值：%s",
            ", ".join(sorted(unknown)),
            ", ".join(sorted(ALL_FEATURES)),
        )
    if "plan" in feats and "file" not in feats:
        feats.discard("plan")
        logger.warning("feature 'plan' 依赖 'file'，当前未启用 file，已丢弃 plan")
    return feats
