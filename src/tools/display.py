"""工具展示数据结构与格式化 — 供事件和 UI 消费，不影响 LLM 结果。"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ToolDisplay:
    """工具展示数据 — 传入事件供 UI 消费，不影响 LLM 结果。"""
    title: str          # 中文动作标题，如"执行命令"、"• 已编辑 path (+3 -1)"
    content: str = ""   # 格式化的参数或结果文本
    content_type: str = "text"  # "text" | "diff" | "json"
    truncated: bool = False


@dataclass
class ToolResult:
    """工具函数返回值包装 — 携带展示侧信息，不改变 LLM 侧结果。"""
    text: str                           # 原始字符串结果，交给 LLM
    display: ToolDisplay | None = None  # 仅 UI 消费


# ---------------------------------------------------------------------------
# 内置工具中文标题映射
# ---------------------------------------------------------------------------

TOOL_TITLES: dict[str, str] = {
    # shell
    "shell": "执行命令",
    # 文件工具
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file_lines": "编辑文件",
    "replace_all_in_file": "全局替换",
    "list_directory": "列出目录",
    "glob": "查找文件",
    "grep": "搜索内容",
    "get_file_info": "文件信息",
    "create_directory": "创建目录",
    "move_file": "移动文件",
    # 网络工具
    "web_fetch": "获取网页",
    "web_search": "搜索网页",
    # 子 agent
    "task_delegator": "委派任务",
    "load_skill": "加载技能",
    "ask_user": "询问用户",
    # 计划工具
    "enter_plan_mode": "进入计划模式",
    "set_plan_file": "设置计划文件",
    "exit_plan_mode": "提交计划",
    # 记忆工具
    "save_memory": "保存记忆",
    "read_memory": "读取记忆",
    # 任务工具
    "task_create": "创建任务",
    "task_update": "更新任务",
    "task_list": "任务列表",
    "task_get": "获取任务",
    # 工具类
    "calculator": "计算",
    "compact": "压缩上下文",
    "read_tool_result": "读取分页结果",
    # 实用工具
    "random": "随机生成",
    "datetime": "日期时间",
    "encode": "编码转换",
    "text_stats": "文本统计",
}


def tool_title(tool_name: str) -> str:
    """返回工具中文标题；未命中映射时返回 '调用 {tool_name}'。"""
    return TOOL_TITLES.get(tool_name, f"调用 {tool_name}")


# ---------------------------------------------------------------------------
# 参数格式化
# ---------------------------------------------------------------------------

# shell 工具的摘要提取（与 ToolsMgr 现有 shell_summary 兼容）
def _shell_summary(args: dict[str, Any]) -> str:
    """提取 shell 工具的命令摘要。"""
    cmd = args.get("command", "")
    lines = cmd.strip().splitlines()
    if len(lines) <= 3:
        return cmd.strip()
    return "\n".join(lines[:3]) + f"\n… 共 {len(lines)} 行"


def format_params(tool_name: str, args: dict[str, Any],
                  budget_lines: int = 20, budget_bytes: int = 4096) -> str:
    """格式化工具参数为展示文本。

    已知内置工具按自然语言摘要，未知/MCP 工具输出格式化 JSON。
    """
    if tool_name == "shell":
        return _shell_summary(args)

    if tool_name == "read_file":
        return args.get("path", "")

    if tool_name in ("write_file", "edit_file_lines", "replace_all_in_file"):
        return args.get("path", args.get("file_path", ""))

    if tool_name == "grep":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        return f"{pattern}  in {path}"

    if tool_name == "glob":
        return args.get("pattern", "")

    if tool_name == "list_directory":
        return args.get("path", ".")

    if tool_name == "web_search":
        query = args.get("query", "")
        if len(query) > 100:
            query = query[:100] + "…"
        return query

    if tool_name == "web_fetch":
        return args.get("url", "")

    if tool_name == "task_delegator":
        desc = args.get("description", "")
        if len(desc) > 120:
            desc = desc[:120] + "…"
        return desc

    if tool_name == "load_skill":
        return args.get("name", "")

    # 文件工具（已有路径的补充）
    if tool_name == "get_file_info":
        return args.get("path", "")

    if tool_name == "create_directory":
        return args.get("path", "")

    if tool_name == "move_file":
        src = args.get("source", "")
        dst = args.get("destination", "")
        return f"{src} → {dst}"

    # 记忆工具
    if tool_name == "save_memory":
        return args.get("title", "")

    if tool_name == "read_memory":
        return args.get("title", "")

    # 计划工具
    if tool_name == "enter_plan_mode":
        return ""

    if tool_name in ("set_plan_file", "exit_plan_mode"):
        return args.get("file_path", "")

    # 工具类
    if tool_name == "calculator":
        return args.get("expression", "")

    if tool_name == "compact":
        focus = args.get("focus", "")
        if len(focus) > 80:
            focus = focus[:80] + "…"
        return focus

    if tool_name == "read_tool_result":
        return f"第 {args.get('page', 2)} 页"

    # 实用工具
    if tool_name in ("random", "datetime", "encode", "text_stats"):
        return args.get("operation", "")

    # 任务工具
    if tool_name == "task_create":
        return args.get("subject", "")

    if tool_name == "task_update":
        tid = args.get("task_id", "")
        status = args.get("status", "")
        return f"#{tid} → {status}" if status else f"#{tid}"

    if tool_name == "task_list":
        return ""

    if tool_name == "task_get":
        return f"#{args.get('task_id', '')}"

    # 未知/MCP 工具：格式化 JSON
    try:
        text = json.dumps(args, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(args)

    return _truncate_text(text, budget_lines, budget_bytes)


# ---------------------------------------------------------------------------
# 结果格式化
# ---------------------------------------------------------------------------

def format_result(content: str, budget_lines: int = 60,
                  budget_bytes: int = 12288) -> tuple[str, bool]:
    """截断结果内容并返回 (截断后文本, 是否截断)。"""
    if not content:
        return "", False
    return _truncate_text_with_flag(content, budget_lines, budget_bytes)


# ---------------------------------------------------------------------------
# 文件差异
# ---------------------------------------------------------------------------

def build_file_diff(old_lines: list[str], new_lines: list[str],
                    display_path: str) -> ToolDisplay:
    """用 difflib.SequenceMatcher 生成分组差异（2 行上下文）。

    返回 ToolDisplay，标题为 '• 已编辑 {path} (+A -D)'，content_type='diff'。
    """
    added = 0
    deleted = 0
    diff_chunks: list[str] = []

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    context = 2

    for group in matcher.get_grouped_opcodes(context):
        chunk_lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for idx in range(i1, i2):
                    line = old_lines[idx].rstrip("\n\r")
                    chunk_lines.append(f"    {idx + 1:>4}  {line}")
            elif tag == "delete":
                for idx in range(i1, i2):
                    line = old_lines[idx].rstrip("\n\r")
                    chunk_lines.append(f"  - {idx + 1:>4}  {line}")
                    deleted += 1
            elif tag == "insert":
                for idx in range(j1, j2):
                    line = new_lines[idx].rstrip("\n\r")
                    chunk_lines.append(f"  + {idx + 1:>4}  {line}")
                    added += 1
            elif tag == "replace":
                for idx in range(i1, i2):
                    line = old_lines[idx].rstrip("\n\r")
                    chunk_lines.append(f"  - {idx + 1:>4}  {line}")
                    deleted += 1
                for idx in range(j1, j2):
                    line = new_lines[idx].rstrip("\n\r")
                    chunk_lines.append(f"  + {idx + 1:>4}  {line}")
                    added += 1
        diff_chunks.append("\n".join(chunk_lines))

    # 短路径
    path = _short_path(display_path)
    title = f"• 已编辑 {path} (+{added} -{deleted})"

    if not diff_chunks:
        return ToolDisplay(title=title, content="（无变更）", content_type="diff")

    content = "\n  ···\n".join(diff_chunks)

    # 限额
    content, truncated = _truncate_text_with_flag(content, 60, 12288)

    return ToolDisplay(
        title=title,
        content=content,
        content_type="diff",
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _short_path(path: str) -> str:
    """缩短过长的路径显示。"""
    parts = PurePosixPath(path).parts
    if len(parts) <= 4:
        return path
    return str(PurePosixPath(*parts[:1], "…", *parts[-2:]))


def _truncate_text(text: str, max_lines: int, max_bytes: int) -> str:
    """截断文本到行数和字节限额。"""
    result, _ = _truncate_text_with_flag(text, max_lines, max_bytes)
    return result


def _truncate_text_with_flag(text: str, max_lines: int,
                             max_bytes: int) -> tuple[str, bool]:
    """截断文本到行数和字节限额，返回 (结果, 是否截断)。"""
    lines = text.splitlines()
    truncated = False

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    result = "\n".join(lines)
    encoded = result.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        # 按字节截断，保持 UTF-8 完整
        result = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    if truncated:
        result += "\n… (已截断)"

    return result, truncated
