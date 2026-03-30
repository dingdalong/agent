"""工具分类配置加载与校验。

从 tool_categories.json 中加载分类树，展平为叶子节点映射，
并校验分类的完整性与一致性。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Required, TypedDict

logger = logging.getLogger(__name__)


class CategoryEntry(TypedDict, total=False):
    """叶子类别条目，包含 description、tools，以及可选的 instructions。"""

    description: Required[str]
    tools: Required[list[str]]
    instructions: str


def load_categories(config_path: str | Path) -> dict[str, CategoryEntry]:
    """加载 tool_categories.json，返回叶子类别映射。

    返回 dict: agent_name -> CategoryEntry
    agent_name 以 ``tool_`` 为前缀，嵌套子类别用下划线拼接，
    例如 ``tool_text_editing_code_editing``。

    如果配置文件不存在或 JSON 格式有误，返回空字典。
    """
    path = Path(config_path)
    if not path.exists():
        logger.warning("分类配置 %s 不存在", config_path)
        return {}

    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            logger.warning("分类配置 %s JSON 解析失败: %s", config_path, exc)
            return {}

    categories = data.get("categories", {})
    return _flatten_categories(categories, prefix="tool")


def _flatten_categories(
    categories: dict[str, Any], prefix: str
) -> dict[str, CategoryEntry]:
    """递归展开分类树，只保留叶子节点（含 tools 字段的节点）。

    非叶子节点（含 subcategories）会继续递归，其自身不出现在结果中。
    若节点同时含有 subcategories 和 tools，优先递归 subcategories 并记录警告。
    若节点既不含 subcategories 也不含 tools，记录警告并跳过。
    """
    result: dict[str, CategoryEntry] = {}
    for name, cat in categories.items():
        agent_name = f"{prefix}_{name}"
        if "subcategories" in cat:
            # 若同时含有 tools，数据存在歧义，记录警告并以 subcategories 优先
            if "tools" in cat:
                logger.warning(
                    "类别 %s 同时含有 subcategories 和 tools，将忽略 tools 并继续递归子分类",
                    agent_name,
                )
            # 非叶子：递归处理子分类
            sub = _flatten_categories(cat["subcategories"], prefix=agent_name)
            result.update(sub)
        elif "tools" in cat:
            # 叶子节点
            entry: CategoryEntry = {
                "description": cat["description"],
                "tools": list(cat["tools"]),
            }
            if "instructions" in cat:
                entry["instructions"] = cat["instructions"]
            result[agent_name] = entry
        else:
            # 既无子分类也无工具列表，可能是配置拼写错误
            logger.warning(
                "类别 %s 既没有 subcategories 也没有 tools，已跳过（请检查配置是否有拼写错误）",
                agent_name,
            )
    return result


def validate_categories(
    categories: dict[str, CategoryEntry],
    all_tool_names: set[str],
) -> list[str]:
    """校验分类配置的完整性和一致性。

    校验规则：
    - 每个工具必须恰好出现在一个类别中（全覆盖、无重复）
    - 类别名（去掉 ``tool_`` 前缀后）必须是合法的 snake_case
    - description 不能为空或纯空白
    - 引用的工具必须存在于 all_tool_names 中

    返回错误列表，空列表表示校验通过。
    """
    errors: list[str] = []
    seen_tools: dict[str, str] = {}  # tool_name -> category_name
    categorized_tools: set[str] = set()

    for cat_name, cat in categories.items():
        # 校验 description 非空（含纯空白字符）
        if not cat.get("description", "").strip():
            errors.append(f"类别 {cat_name} 缺少 description")

        # 校验类别名 snake_case
        raw_name = cat_name.removeprefix("tool_")
        if not re.match(r"^[a-z][a-z0-9_]*$", raw_name):
            errors.append(f"类别名 {cat_name} 不合法（需要 snake_case）")

        # 校验工具：存在性与唯一性
        for tool_name in cat.get("tools", []):
            if tool_name in seen_tools:
                errors.append(
                    f"工具 {tool_name} 重复出现在 {seen_tools[tool_name]} 和 {cat_name}"
                )
            seen_tools[tool_name] = cat_name
            categorized_tools.add(tool_name)

            if tool_name not in all_tool_names:
                errors.append(f"工具 {tool_name} 不存在于当前已注册的工具中")

    # 校验全覆盖：每个已注册的工具都必须被分类
    missing = all_tool_names - categorized_tools
    for tool_name in sorted(missing):
        errors.append(f"工具 {tool_name} 未被分配到任何类别")

    return errors
