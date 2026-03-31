"""tool_categories.json 配置加载与校验测试。"""
import json
import pytest
from pathlib import Path


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "terminal": {
                "description": "终端操作",
                "tools": {
                    "execute_command": "Execute a shell command",
                    "read_output": "Read command output",
                },
            },
            "calculation": {
                "description": "数学计算",
                "tools": {"calculate": "Perform math calculation"},
            },
        },
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")
    return p


@pytest.fixture
def nested_config(tmp_path: Path) -> Path:
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "text_editing": {
                "description": "文本编辑",
                "subcategories": {
                    "code_editing": {
                        "description": "代码编辑",
                        "tools": {
                            "edit_block": "Edit a code block",
                            "search_code": "Search in code",
                        },
                    },
                    "document_editing": {
                        "description": "文档编辑",
                        "tools": {"find_replace": "Find and replace text"},
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
    assert result["tool_terminal"]["tools"] == {
        "execute_command": "Execute a shell command",
        "read_output": "Read command output",
    }
    assert "tool_calculation" in result


def test_load_categories_nested(nested_config: Path):
    from src.tools.categories import load_categories
    result = load_categories(str(nested_config))
    assert "tool_text_editing" not in result
    assert "tool_text_editing_code_editing" in result
    assert result["tool_text_editing_code_editing"]["tools"] == {
        "edit_block": "Edit a code block",
        "search_code": "Search in code",
    }
    assert "tool_text_editing_document_editing" in result


def test_load_categories_missing_file():
    from src.tools.categories import load_categories
    result = load_categories("/nonexistent/path.json")
    assert result == {}


def test_validate_categories_all_tools_covered():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": {"t1": "Tool 1", "t2": "Tool 2"}},
        "tool_b": {"description": "B", "tools": {"t3": "Tool 3"}},
    }
    errors = validate_categories(categories, {"t1", "t2", "t3"})
    assert errors == []


def test_validate_categories_missing_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": {"t1": "Tool 1"}}}
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_duplicate_tools():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": {"t1": "Tool 1", "t2": "Tool 2"}},
        "tool_b": {"description": "B", "tools": {"t2": "Tool 2 again"}},
    }
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_unknown_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": {"t1": "Tool 1", "unknown": "Unknown tool"}}}
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
                "tools": {"execute_command": "Execute a shell command"},
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
        "tool_Bad-Name": {"description": "错误命名示例", "tools": {"t1": "Tool 1"}},
    }
    errors = validate_categories(categories, {"t1"})
    assert any("Bad-Name" in e or "tool_Bad-Name" in e for e in errors)


# ---------------------------------------------------------------------------
# CategoryResolver 测试
# ---------------------------------------------------------------------------


def test_category_resolver_can_resolve():
    """can_resolve 对已知类别返回 True，未知类别返回 False。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)
    assert resolver.can_resolve("tool_terminal") is True
    assert resolver.can_resolve("tool_unknown") is False


def test_category_resolver_get_category():
    """get_category 返回原始 CategoryEntry，不存在时返回 None。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)

    cat = resolver.get_category("tool_terminal")
    assert cat is not None
    assert cat["description"] == "终端操作"
    assert cat["tools"] == {"exec": "Execute", "read": "Read"}

    assert resolver.get_category("tool_unknown") is None


def test_category_resolver_build_instructions_default():
    """无自定义 instructions 时，使用模板自动生成。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)
    instructions = resolver.build_instructions("tool_terminal")

    assert "终端操作" in instructions
    assert "exec" in instructions
    assert "read" in instructions


def test_category_resolver_build_instructions_custom():
    """有自定义 instructions 时，直接使用而非模板。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {
            "description": "终端操作",
            "tools": {"exec": "Execute"},
            "instructions": "自定义指令",
        }
    }
    resolver = CategoryResolver(cats)
    assert resolver.build_instructions("tool_terminal") == "自定义指令"


def test_category_resolver_build_instructions_unknown_raises():
    """对未知类别调用 build_instructions 应抛出 KeyError。"""
    from src.tools.categories import CategoryResolver

    resolver = CategoryResolver({})
    with pytest.raises(KeyError):
        resolver.build_instructions("tool_nonexistent")


def test_category_resolver_get_all_summaries():
    """get_all_summaries 返回所有类别的 name 和 description。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    resolver = CategoryResolver(cats)
    summaries = resolver.get_all_summaries()

    assert len(summaries) == 2
    names = {s["name"] for s in summaries}
    assert names == {"tool_terminal", "tool_calc"}
    descs = {s["description"] for s in summaries}
    assert descs == {"终端操作", "计算"}


# ---------------------------------------------------------------------------
# Task 2: get_delegate_names() 测试
# ---------------------------------------------------------------------------


def test_category_resolver_get_delegate_names_excludes_self():
    """get_delegate_names 返回其他分类的 delegate 工具名，排除自身。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
        "tool_files": {"description": "文件操作", "tools": {"read": "Read file"}},
    }
    resolver = CategoryResolver(cats)
    names = resolver.get_delegate_names(exclude="tool_terminal")

    assert "delegate_tool_calc" in names
    assert "delegate_tool_files" in names
    assert "delegate_tool_terminal" not in names
    assert len(names) == 2


def test_category_resolver_get_delegate_names_single_category():
    """只有一个分类时，排除自身后返回空列表。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_only": {"description": "唯一", "tools": {"t1": "Tool"}}}
    resolver = CategoryResolver(cats)
    names = resolver.get_delegate_names(exclude="tool_only")
    assert names == []


# ---------------------------------------------------------------------------
# Task 3: build_instructions() with delegate support 测试
# ---------------------------------------------------------------------------


def test_category_resolver_build_instructions_with_delegates():
    """传入 delegate_summaries 时，指令中包含协作能力段落。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    resolver = CategoryResolver(cats)
    delegate_summaries = [{"name": "tool_calc", "description": "计算"}]
    instructions = resolver.build_instructions("tool_terminal", delegate_summaries=delegate_summaries)

    assert "终端操作" in instructions
    assert "exec" in instructions
    assert "协作能力" in instructions
    assert "delegate_tool_calc" in instructions
    assert "计算" in instructions


def test_category_resolver_build_instructions_without_delegates():
    """不传 delegate_summaries 时，不包含协作能力段落（向后兼容）。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}}}
    resolver = CategoryResolver(cats)
    instructions = resolver.build_instructions("tool_terminal")

    assert "终端操作" in instructions
    assert "协作能力" not in instructions


def test_category_resolver_build_instructions_custom_ignores_delegates():
    """有自定义 instructions 时，delegate_summaries 不影响结果。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {
            "description": "终端操作",
            "tools": {"exec": "Execute"},
            "instructions": "自定义指令",
        }
    }
    resolver = CategoryResolver(cats)
    instructions = resolver.build_instructions(
        "tool_terminal",
        delegate_summaries=[{"name": "tool_calc", "description": "计算"}],
    )
    assert instructions == "自定义指令"
