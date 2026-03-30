"""CLI 分类入口测试。"""
import json
import pytest
from pathlib import Path


def test_detect_changes_no_existing_config():
    from src.tools.classify import detect_changes
    changed, added, removed = detect_changes({"t1", "t2", "t3"}, None)
    assert changed is True
    assert added == {"t1", "t2", "t3"}
    assert removed == set()


def test_detect_changes_no_change(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {
            "cat_a": {"description": "A", "tools": ["t1", "t2"]},
            "cat_b": {"description": "B", "tools": ["t3"]},
        },
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2", "t3"}, str(config_path))
    assert changed is False
    assert added == set()
    assert removed == set()


def test_detect_changes_new_tools(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {"cat_a": {"description": "A", "tools": ["t1"]}},
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2"}, str(config_path))
    assert changed is True
    assert added == {"t2"}


def test_detect_changes_removed_tools(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {"cat_a": {"description": "A", "tools": ["t1", "t2"]}},
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1"}, str(config_path))
    assert changed is True
    assert removed == {"t2"}


def test_detect_changes_with_nested_config(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {
            "text_editing": {
                "description": "编辑",
                "subcategories": {
                    "code": {"description": "代码编辑", "tools": ["t1"]},
                    "doc": {"description": "文档编辑", "tools": ["t2"]},
                },
            },
        },
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2"}, str(config_path))
    assert changed is False


def test_build_output():
    from src.tools.classify import _build_output
    categories = {
        "tool_terminal": {"description": "终端", "tools": ["exec", "read"]},
        "tool_calc": {"description": "计算", "tools": ["calc"]},
    }
    output = _build_output(categories, max_per_category=8)
    assert output["version"] == 1
    assert output["max_tools_per_category"] == 8
    assert "terminal" in output["categories"]
    assert "calc" in output["categories"]
    assert output["categories"]["terminal"]["tools"] == ["exec", "read"]
