"""src/tools/display.py 的单元测试：格式化函数、diff 生成、ToolResult 保留行为。"""

from __future__ import annotations

from src.tools.display import (
    TOOL_TITLES,
    ToolDisplay,
    ToolResult,
    build_file_diff,
    format_params,
    format_result,
    tool_title,
)


# ---------------------------------------------------------------------------
# tool_title — 中文标题映射
# ---------------------------------------------------------------------------

def test_tool_title_known():
    assert tool_title("shell") == "执行命令"
    assert tool_title("read_file") == "读取文件"
    assert tool_title("write_file") == "写入文件"
    assert tool_title("edit_file_lines") == "编辑文件"
    assert tool_title("replace_all_in_file") == "全局替换"
    assert tool_title("grep") == "搜索内容"
    assert tool_title("glob") == "查找文件"
    assert tool_title("web_fetch") == "获取网页"
    assert tool_title("web_search") == "搜索网页"


def test_tool_title_file_tools():
    assert tool_title("get_file_info") == "文件信息"
    assert tool_title("create_directory") == "创建目录"
    assert tool_title("move_file") == "移动文件"


def test_tool_title_plan_tools():
    assert tool_title("enter_plan_mode") == "进入计划模式"
    assert tool_title("set_plan_file") == "设置计划文件"
    assert tool_title("exit_plan_mode") == "提交计划"


def test_tool_title_memory_tools():
    assert tool_title("save_memory") == "保存记忆"
    assert tool_title("read_memory") == "读取记忆"


def test_tool_title_task_tools():
    assert tool_title("task_create") == "创建任务"
    assert tool_title("task_update") == "更新任务"
    assert tool_title("task_list") == "任务列表"
    assert tool_title("task_get") == "获取任务"


def test_tool_title_utility_tools():
    assert tool_title("calculator") == "计算"
    assert tool_title("compact") == "压缩上下文"
    assert tool_title("read_tool_result") == "读取分页结果"
    assert tool_title("random") == "随机生成"
    assert tool_title("datetime") == "日期时间"
    assert tool_title("encode") == "编码转换"
    assert tool_title("text_stats") == "文本统计"


def test_tool_title_unknown_fallback():
    assert tool_title("unknown_mcp_tool") == "调用 unknown_mcp_tool"


# ---------------------------------------------------------------------------
# format_params — 参数格式化
# ---------------------------------------------------------------------------

def test_format_params_shell():
    result = format_params("shell", {"command": "ls -la"})
    assert "ls -la" in result


def test_format_params_shell_multiline():
    cmd = "\n".join(f"echo line{i}" for i in range(10))
    result = format_params("shell", {"command": cmd})
    assert "共 10 行" in result


def test_format_params_read_file():
    assert format_params("read_file", {"path": "/foo/bar.py"}) == "/foo/bar.py"


def test_format_params_write_file():
    assert format_params("write_file", {"path": "/foo/bar.py"}) == "/foo/bar.py"


def test_format_params_grep():
    result = format_params("grep", {"pattern": "TODO", "path": "src/"})
    assert "TODO" in result
    assert "src/" in result


def test_format_params_glob():
    assert format_params("glob", {"pattern": "*.py"}) == "*.py"


def test_format_params_web_external_read():
    # EXTERNAL_READ 工具不记录查询内容
    assert format_params("web_search", {"query": "secret query"}) == ""
    assert format_params("web_fetch", {"url": "http://secret.com"}) == ""


def test_format_params_task_delegator():
    """task_delegator 使用 description 字段。"""
    result = format_params("task_delegator", {"description": "搜索代码"})
    assert result == "搜索代码"


def test_format_params_task_delegator_truncate():
    long_desc = "a" * 200
    result = format_params("task_delegator", {"description": long_desc})
    assert len(result) <= 121 + len("…")
    assert result.endswith("…")


def test_format_params_load_skill():
    """load_skill 使用 name 字段。"""
    assert format_params("load_skill", {"name": "godot-ui"}) == "godot-ui"


def test_format_params_file_extra_tools():
    assert format_params("get_file_info", {"path": "/foo.py"}) == "/foo.py"
    assert format_params("create_directory", {"path": "/new_dir"}) == "/new_dir"
    result = format_params("move_file", {"source": "a.py", "destination": "b.py"})
    assert "a.py" in result
    assert "b.py" in result
    assert "→" in result


def test_format_params_memory_tools():
    assert format_params("save_memory", {"title": "项目约定"}) == "项目约定"
    assert format_params("read_memory", {"title": "编码风格"}) == "编码风格"


def test_format_params_plan_tools():
    assert format_params("enter_plan_mode", {}) == ""
    assert format_params("set_plan_file", {"file_path": "/plans/foo.md"}) == "/plans/foo.md"
    assert format_params("exit_plan_mode", {"file_path": "/plans/foo.md"}) == "/plans/foo.md"


def test_format_params_calculator():
    assert format_params("calculator", {"expression": "2 + 3"}) == "2 + 3"


def test_format_params_compact():
    assert format_params("compact", {"focus": "保留测试用例"}) == "保留测试用例"


def test_format_params_compact_truncate():
    long_focus = "保留" * 100
    result = format_params("compact", {"focus": long_focus})
    assert result.endswith("…")


def test_format_params_read_tool_result():
    assert format_params("read_tool_result", {"page": 3}) == "第 3 页"


def test_format_params_utility_tools():
    assert format_params("random", {"operation": "uuid"}) == "uuid"
    assert format_params("datetime", {"operation": "now"}) == "now"
    assert format_params("encode", {"operation": "base64_encode"}) == "base64_encode"
    assert format_params("text_stats", {"operation": "char_count"}) == "char_count"


def test_format_params_task_tools():
    assert format_params("task_create", {"subject": "修复 bug"}) == "修复 bug"
    result = format_params("task_update", {"task_id": "1", "status": "completed"})
    assert "#1" in result
    assert "completed" in result
    # 无 status 时只显示 id
    assert format_params("task_update", {"task_id": "2"}) == "#2"
    assert format_params("task_list", {}) == ""
    assert format_params("task_get", {"task_id": "3"}) == "#3"


def test_format_params_unknown_json():
    result = format_params("mcp_tool", {"foo": "bar", "count": 42})
    assert "foo" in result
    assert "bar" in result


# ---------------------------------------------------------------------------
# format_result — 结果截断
# ---------------------------------------------------------------------------

def test_format_result_short():
    text, truncated = format_result("hello world")
    assert text == "hello world"
    assert not truncated


def test_format_result_empty():
    text, truncated = format_result("")
    assert text == ""
    assert not truncated


def test_format_result_line_limit():
    lines = "\n".join(f"line {i}" for i in range(100))
    text, truncated = format_result(lines, budget_lines=10)
    assert truncated
    assert "已截断" in text
    # 截断后不超过 10 行 + 截断提示行
    assert text.count("\n") <= 11


def test_format_result_byte_limit():
    text_input = "x" * 200
    text, truncated = format_result(text_input, budget_bytes=100)
    assert truncated
    assert len(text.encode("utf-8")) <= 200  # 100 + 截断提示


def test_format_result_unicode_safe():
    # 确保 UTF-8 截断不会产生不完整字符
    text_input = "你好世界" * 100
    text, truncated = format_result(text_input, budget_bytes=50)
    assert truncated
    text.encode("utf-8")  # 不应抛异常


# ---------------------------------------------------------------------------
# build_file_diff — 文件差异
# ---------------------------------------------------------------------------

def test_build_file_diff_no_change():
    lines = ["hello\n", "world\n"]
    display = build_file_diff(lines, lines, "test.py")
    assert display.content_type == "diff"
    assert "（无变更）" in display.content
    assert "+0" in display.title
    assert "-0" in display.title


def test_build_file_diff_new_file():
    old: list[str] = []
    new = ["line1\n", "line2\n", "line3\n"]
    display = build_file_diff(old, new, "new_file.py")
    assert "+3" in display.title
    assert "-0" in display.title
    # git diff 风格：行首 + 标记
    assert "  + " in display.content


def test_build_file_diff_delete_lines():
    old = ["a\n", "b\n", "c\n"]
    new = ["a\n", "c\n"]
    display = build_file_diff(old, new, "test.py")
    assert "-1" in display.title
    # git diff 风格：行首 - 标记
    assert "  - " in display.content


def test_build_file_diff_replace():
    old = ["line1\n", "old_line\n", "line3\n"]
    new = ["line1\n", "new_line\n", "line3\n"]
    display = build_file_diff(old, new, "test.py")
    assert "+1" in display.title
    assert "-1" in display.title
    assert "  + " in display.content
    assert "  - " in display.content


def test_build_file_diff_path_in_title():
    display = build_file_diff(["a\n"], ["b\n"], "src/foo/bar.py")
    assert "src/foo/bar.py" in display.title


def test_build_file_diff_stats():
    old = ["a\n"]
    new = ["b\n", "c\n", "d\n"]
    display = build_file_diff(old, new, "f.py")
    # 1 行删除，3 行新增
    assert "+3" in display.title
    assert "-1" in display.title


# ---------------------------------------------------------------------------
# ToolResult — 数据结构
# ---------------------------------------------------------------------------

def test_tool_result_basic():
    tr = ToolResult(text="ok")
    assert tr.text == "ok"
    assert tr.display is None


def test_tool_result_with_display():
    display = ToolDisplay(title="test", content="body")
    tr = ToolResult(text="ok", display=display)
    assert tr.display is display
    assert tr.display.title == "test"


# ---------------------------------------------------------------------------
# ToolResult 在 ToolEntry.__call__ 中的保留行为
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import MagicMock
from pydantic import BaseModel
from src.tools.decorator import ToolEntry


class _EmptyModel(BaseModel):
    pass


def test_tool_entry_preserves_tool_result():
    """ToolEntry.__call__ 对返回 ToolResult 的函数保留原对象而非 str()。"""
    display = ToolDisplay(title="写入文件", content="diff...")
    expected = ToolResult(text="done", display=display)

    def my_tool():
        return expected

    entry = ToolEntry(
        name="test_tool",
        func=my_tool,
        model=_EmptyModel,
        description="test",
        parameters_schema={"type": "object", "properties": {}},
    )
    result = asyncio.run(
        entry({"deps": None, "agent": None}, validated=True)
    )
    assert isinstance(result, ToolResult)
    assert result.text == "done"
    assert result.display is display


def test_tool_entry_str_for_normal_return():
    """ToolEntry.__call__ 对普通返回值仍 str()。"""
    def my_tool():
        return 42

    entry = ToolEntry(
        name="test_tool",
        func=my_tool,
        model=_EmptyModel,
        description="test",
        parameters_schema={"type": "object", "properties": {}},
    )
    result = asyncio.run(
        entry({"deps": None, "agent": None}, validated=True)
    )
    assert result == "42"


# ---------------------------------------------------------------------------
# TOOL_TITLES 完整性
# ---------------------------------------------------------------------------

def test_tool_titles_all_have_values():
    for name, title in TOOL_TITLES.items():
        assert title, f"TOOL_TITLES['{name}'] 不应为空"
        assert isinstance(title, str)
