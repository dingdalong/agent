"""工具分类 CLI 入口。

用法：
    uv run python -m src.tools.classify          # 全量分类
    uv run python -m src.tools.classify --force   # 强制重分类
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from src.config import load_config, AppConfig
from src.tools.decorator import get_registry
from src.tools.discovery import discover_tools
from src.tools.classifier import classify_tools
from src.tools.categories import validate_categories

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = "tool_categories.json"


def detect_changes(
    current_tools: set[str],
    config_path: str | None,
) -> tuple[bool, set[str], set[str]]:
    """对比当前工具列表与现有配置，返回 (has_change, added, removed)。"""
    if config_path is None or not Path(config_path).exists():
        return True, current_tools, set()

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    existing_tools: set[str] = set()
    _collect_tools(data.get("categories", {}), existing_tools)

    added = current_tools - existing_tools
    removed = existing_tools - current_tools
    return bool(added or removed), added, removed


def _collect_tools(categories: dict, out: set[str]) -> None:
    """递归收集配置中所有工具名。支持 dict 和 list 两种 tools 格式。"""
    for cat in categories.values():
        if "tools" in cat:
            tools = cat["tools"]
            if isinstance(tools, dict):
                out.update(tools.keys())
            else:
                out.update(tools)
        if "subcategories" in cat:
            _collect_tools(cat["subcategories"], out)


def _build_output(
    categories: dict[str, dict[str, Any]],
    max_per_category: int,
) -> dict[str, Any]:
    """将叶子映射转为输出 JSON 格式（version 2，tools 为 dict）。"""
    raw_categories: dict[str, Any] = {}
    for agent_name, cat in sorted(categories.items()):
        path = agent_name.removeprefix("tool_")
        raw_categories[path] = {
            "description": cat["description"],
            "tools": dict(cat["tools"]),
        }
    return {
        "version": 2,
        "max_tools_per_category": max_per_category,
        "categories": raw_categories,
    }


async def run_classify(force: bool = False, output: str = DEFAULT_OUTPUT) -> None:
    """分类主流程。"""
    config = load_config()
    raw = config.raw

    # 1. Local tools
    discover_tools("src.tools.builtin", config.resolve("src/tools/builtin"))
    local_schemas = get_registry().get_schemas()

    # 2. MCP tools
    mcp_schemas: list[dict] = []
    mcp_manager = None
    try:
        from src.mcp.config import load_mcp_config
        from src.mcp.manager import MCPManager

        mcp_config_path = str(config.resolve(raw.get("mcp", {}).get("config_path", "mcp_servers.json")))
        mcp_manager = MCPManager(configs=load_mcp_config(mcp_config_path))
        await mcp_manager.connect_all()
        mcp_schemas = mcp_manager.get_tools_schemas()
    except Exception:
        logger.warning("MCP 连接失败，仅使用本地工具", exc_info=True)

    try:
        all_schemas = local_schemas + mcp_schemas
        all_tool_names = {s["function"]["name"] for s in all_schemas}

        if not all_schemas:
            print("未发现任何工具，退出。")
            return

        # 3. Change detection
        if not force:
            changed, added, removed = detect_changes(all_tool_names, output)
            if not changed:
                print("工具列表无变化，跳过分类。使用 --force 强制重分类。")
                return
            if added:
                print(f"新增工具：{', '.join(sorted(added))}")
            if removed:
                print(f"移除工具：{', '.join(sorted(removed))}")

        # 4. LLM classification
        llm_cfg = raw.get("llm", {})
        from src.llm.openai import OpenAIProvider

        llm = OpenAIProvider(
            api_key=llm_cfg.get("api_key", ""),
            base_url=llm_cfg.get("base_url", ""),
            model=llm_cfg.get("model", ""),
        )
        max_per_category = raw.get("tools", {}).get("max_tools_per_category", 8)
        print(f"正在分类 {len(all_schemas)} 个工具（上限 {max_per_category}/类别）...")
        categories = await classify_tools(all_schemas, llm, max_per_category)

        # 5. Validate
        errors = validate_categories(categories, all_tool_names)
        if errors:
            print("分类校验失败：")
            for e in errors:
                print(f"  - {e}")
            print("未写入配置。")
            return

        # 6. Write output
        output_data = _build_output(categories, max_per_category)
        with open(output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        cat_count = len(categories)
        tool_count = sum(len(c["tools"]) for c in categories.values())
        print(f"\n分类结果已写入 {output}，请 review 后提交。")
        print(f"共 {cat_count} 个类别，{tool_count} 个工具。")

    finally:
        # 7. Cleanup — 无论是否出错都断开 MCP 连接，避免连接泄漏
        if mcp_manager:
            await mcp_manager.disconnect_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="工具统一分类")
    parser.add_argument("--force", action="store_true", help="强制重分类")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出文件路径")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_classify(force=args.force, output=args.output))


if __name__ == "__main__":
    main()
