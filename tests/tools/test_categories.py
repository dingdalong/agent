"""tool_categories.json 配置加载与校验测试。"""
import json
import pytest
from pathlib import Path


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {
            "terminal": {
                "description": "终端操作",
                "tools": ["execute_command", "read_output"],
            },
            "calculation": {
                "description": "数学计算",
                "tools": ["calculate"],
            },
        },
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")
    return p


@pytest.fixture
def nested_config(tmp_path: Path) -> Path:
    config = {
        "version": 1,
        "max_tools_per_category": 8,
        "categories": {
            "text_editing": {
                "description": "文本编辑",
                "subcategories": {
                    "code_editing": {
                        "description": "代码编辑",
                        "tools": ["edit_block", "search_code"],
                    },
                    "document_editing": {
                        "description": "文档编辑",
                        "tools": ["find_replace"],
                    },
                },
            },
        },
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")
    return p


def test_load_categories_valid(valid_config: Path):
    from src.tools.categories import load_categories
    result = load_categories(str(valid_config))
    assert "tool_terminal" in result
    assert result["tool_terminal"]["description"] == "终端操作"
    assert result["tool_terminal"]["tools"] == ["execute_command", "read_output"]
    assert "tool_calculation" in result


def test_load_categories_nested(nested_config: Path):
    from src.tools.categories import load_categories
    result = load_categories(str(nested_config))
    assert "tool_text_editing" not in result
    assert "tool_text_editing_code_editing" in result
    assert result["tool_text_editing_code_editing"]["tools"] == ["edit_block", "search_code"]
    assert "tool_text_editing_document_editing" in result


def test_load_categories_missing_file():
    from src.tools.categories import load_categories
    result = load_categories("/nonexistent/path.json")
    assert result == {}


def test_validate_categories_all_tools_covered():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": ["t1", "t2"]},
        "tool_b": {"description": "B", "tools": ["t3"]},
    }
    errors = validate_categories(categories, {"t1", "t2", "t3"})
    assert errors == []


def test_validate_categories_missing_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": ["t1"]}}
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_duplicate_tools():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": ["t1", "t2"]},
        "tool_b": {"description": "B", "tools": ["t2"]},
    }
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_unknown_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": ["t1", "unknown"]}}
    errors = validate_categories(categories, {"t1"})
    assert any("unknown" in e for e in errors)


def test_flatten_categories_instructions_passthrough(tmp_path: Path):
    """instructions 字段应完整透传到叶子节点条目。"""
    import json
    from src.tools.categories import load_categories

    config = {
        "categories": {
            "terminal": {
                "description": "终端操作",
                "tools": ["execute_command"],
                "instructions": "只在必要时使用",
            }
        }
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")

    result = load_categories(p)
    assert "tool_terminal" in result
    assert result["tool_terminal"].get("instructions") == "只在必要时使用"


def test_validate_categories_invalid_snake_case_name():
    """类别名包含大写字母或连字符时，应产生校验错误。"""
    from src.tools.categories import validate_categories

    categories = {
        "tool_Bad-Name": {"description": "错误命名示例", "tools": ["t1"]},
    }
    errors = validate_categories(categories, {"t1"})
    assert any("Bad-Name" in e or "tool_Bad-Name" in e for e in errors)
