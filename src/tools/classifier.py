"""工具分类流水线 — 离线分类。

从 MCP 工具描述中提取提示，调用 LLM 进行扁平分类，
校验结果并拆分超额类别。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 匹配工具描述开头的 [Category] 前缀
_HINT_PATTERN = re.compile(r"^\[([A-Za-z_\s]+)\]")


# ---------------------------------------------------------------------------
# LLM Protocol — 仅声明 classify 流水线需要的最小接口
# ---------------------------------------------------------------------------

class _LLM(Protocol):
    """classify_tools 所需的最小 LLM 接口。"""

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# 1. 提取分类提示
# ---------------------------------------------------------------------------

def extract_category_hints(schemas: list[dict[str, Any]]) -> dict[str, str]:
    """从工具描述的 ``[Category]`` 前缀提取分类提示。

    例如 ``[Filesystem] Read a file`` 会提取出 hint ``"Filesystem"``。

    返回 dict: tool_name -> hint_category（仅包含有前缀的工具）。
    """
    hints: dict[str, str] = {}
    for schema in schemas:
        func = schema.get("function", {})
        name = func.get("name", "")
        description = func.get("description", "")
        match = _HINT_PATTERN.match(description)
        if match:
            hints[name] = match.group(1).strip()
    return hints


# ---------------------------------------------------------------------------
# 2. 构建分类 Prompt
# ---------------------------------------------------------------------------

def build_classify_prompt(
    schemas: list[dict[str, Any]],
    hints: dict[str, str],
    max_per_category: int,
) -> str:
    """构建发送给 LLM 的工具分类 prompt。

    列出所有工具及其描述和已知提示，要求 LLM 输出 JSON 格式的分类结果。
    """
    tool_lines: list[str] = []
    for schema in schemas:
        func = schema.get("function", {})
        name = func.get("name", "")
        description = func.get("description", "")
        hint = hints.get(name)
        hint_str = f" (hint: {hint})" if hint else ""
        tool_lines.append(f"- {name}: {description}{hint_str}")

    tools_block = "\n".join(tool_lines)

    return (
        "你是一个工具分类专家。请将以下工具分成若干类别。\n\n"
        f"工具列表：\n{tools_block}\n\n"
        "约束：\n"
        f"- 每个类别最多包含 {max_per_category} 个工具\n"
        "- 合并功能相似的工具到同一类别\n"
        "- 类别名使用 snake_case\n"
        "- 每个工具只能属于一个类别\n\n"
        "- 类别描述要详细，概述该类别包含的所有功能\n\n"
        "请以 JSON 格式输出，格式如下：\n"
        '{"categories": [{"name": "...", "description": "详细的类别描述", '
        '"tools": {"tool_name": "tool_description", ...}}]}\n\n'
        "tools 字段是一个对象，key 是工具名，value 是工具描述。\n"
        "只输出 JSON，不要输出其他内容。"
    )


# ---------------------------------------------------------------------------
# 3. 解析分类响应
# ---------------------------------------------------------------------------

def parse_classify_response(raw: str) -> dict[str, dict[str, Any]]:
    """解析 LLM 的分类响应 JSON。

    支持直接 JSON 和 ````` ```json ``` ````` 代码块两种格式。

    返回叶子类别映射: ``tool_{name}`` -> ``{"description": str, "tools": dict[str, str]}``。

    Raises:
        ValueError: JSON 解析失败。
    """
    data = _extract_json(raw)

    if "categories" not in data:
        raise ValueError("JSON 缺少 'categories' 字段")

    result: dict[str, dict[str, Any]] = {}
    for cat in data["categories"]:
        name = cat["name"]
        key = f"tool_{name}"
        result[key] = {
            "description": cat["description"],
            "tools": dict(cat["tools"]),
        }
    return result


# ---------------------------------------------------------------------------
# 4. 构建拆分 Prompt
# ---------------------------------------------------------------------------

def build_split_prompt(
    category_name: str,
    category: dict[str, Any],
    max_per_category: int,
) -> str:
    """构建拆分超额类别的 prompt。

    当一个类别包含的工具数超过 max_per_category 时，
    要求 LLM 将其拆分成更小的子类别。
    """
    tools_str = ", ".join(f"{name}: {desc}" for name, desc in category["tools"].items())
    return (
        f"类别 \"{category_name}\" 包含太多工具，请将其拆分为更小的子类别。\n\n"
        f"类别描述：{category['description']}\n"
        f"工具列表：{tools_str}\n\n"
        "约束：\n"
        f"- 每个子类别最多包含 {max_per_category} 个工具\n"
        "- 子类别名使用 snake_case\n"
        "- 每个工具只能属于一个子类别\n"
        "- 子类别描述要详细\n\n"
        "请以 JSON 格式输出，格式如下：\n"
        '{"subcategories": [{"name": "...", "description": "详细的子类别描述", '
        '"tools": {"tool_name": "tool_description", ...}}]}\n\n'
        "tools 字段是一个对象，key 是工具名，value 是工具描述。\n"
        "只输出 JSON，不要输出其他内容。"
    )


# ---------------------------------------------------------------------------
# 5. 解析拆分响应
# ---------------------------------------------------------------------------

def parse_split_response(raw: str) -> dict[str, dict[str, Any]]:
    """解析拆分响应 JSON。

    返回子类别映射: ``name`` -> ``{"description": str, "tools": dict[str, str]}``。

    Raises:
        ValueError: JSON 解析失败。
    """
    data = _extract_json(raw)

    if "subcategories" not in data:
        raise ValueError("JSON 缺少 'subcategories' 字段")

    result: dict[str, dict[str, Any]] = {}
    for sub in data["subcategories"]:
        name = sub["name"]
        result[name] = {
            "description": sub["description"],
            "tools": dict(sub["tools"]),
        }
    return result


# ---------------------------------------------------------------------------
# 6. 完整分类流水线
# ---------------------------------------------------------------------------

async def classify_tools(
    schemas: list[dict[str, Any]],
    llm: _LLM,
    max_per_category: int = 8,
) -> dict[str, dict[str, Any]]:
    """完整的工具分类流水线。

    流程：提取提示 -> LLM 分类 -> 解析 -> 溢出拆分。

    Args:
        schemas: 工具 schema 列表（OpenAI function calling 格式）。
        llm: 实现 ``chat()`` 方法的 LLM 提供者。
        max_per_category: 每个类别最多包含的工具数。

    Returns:
        叶子类别映射: ``tool_{name}`` -> ``{"description": str, "tools": dict[str, str]}``。
    """
    if not schemas:
        return {}

    # Step 1: 提取提示
    hints = extract_category_hints(schemas)

    # Step 2: 构建 prompt 并调用 LLM
    prompt = build_classify_prompt(schemas, hints, max_per_category)
    response = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )

    # Step 3: 解析分类结果
    result = parse_classify_response(response.content)

    # Step 4: 拆分超额类别
    overflow_keys = [
        key for key, cat in result.items()
        if len(cat["tools"]) > max_per_category
    ]

    for key in overflow_keys:
        cat = result.pop(key)
        # 去掉 "tool_" 前缀得到原始类别名
        cat_name = key.removeprefix("tool_")

        split_prompt = build_split_prompt(cat_name, cat, max_per_category)
        split_response = await llm.chat(
            messages=[{"role": "user", "content": split_prompt}],
            temperature=0.0,
        )

        subcategories = parse_split_response(split_response.content)
        for sub_name, sub_cat in subcategories.items():
            sub_key = f"{key}_{sub_name}"
            result[sub_key] = sub_cat

    return result


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict[str, Any]:
    """从原始文本中提取 JSON 对象。

    支持直接 JSON 和 ```json ``` 代码块格式。

    Raises:
        ValueError: 无法解析 JSON。
    """
    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试从 ```json ``` 代码块中提取
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 响应中解析 JSON: {raw[:200]}")
