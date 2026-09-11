"""工具展示数据结构与格式化 — 供事件和 UI 消费，不影响 LLM 结果。"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal


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
class FileContent:
    """已按源位置脱敏的文件片段；仅在最终输出整理前保留。"""
    path: str
    lines: list[str]
    total_lines: int
    offset: int
    column: int


@dataclass
class ToolResult:
    """工具函数返回值包装 — 同时携带模型状态、续读位置和展示信息。"""
    text: str
    display: ToolDisplay | None = None
    status: Literal["success", "error", "running", "cancelled"] = "success"
    error_code: str | None = None
    error_details: dict | None = None
    recovery: str | None = None
    stage_results: list[dict] | None = None
    exit_code: int | None = None
    session_id: str | None = None
    artifact_path: str | None = None
    artifact_complete: bool | None = None
    artifact_error: str | None = None
    file_content: FileContent | None = None
    file_range: dict | None = None
    next_read: dict | None = None
    annotations: str = ""
    output_budget: int | None = None  # Hook 重验后的有效预算，不进入模型元数据
    truncated: bool = False
    end_turn: bool = False

    def __str__(self) -> str:
        metadata = {
            key: value for key, value in {
                "status": self.status, "error_code": self.error_code,
                "error_details": self.error_details, "recovery": self.recovery,
                "stage_results": self.stage_results,
                "exit_code": self.exit_code, "session_id": self.session_id,
                "artifact_path": self.artifact_path, "artifact_complete": self.artifact_complete,
                "artifact_error": self.artifact_error,
                "file_range": self.file_range, "next_read": self.next_read,
                "truncated": self.truncated or None,
            }.items() if value is not None
        }
        body = self.text + ("\n\n[工具附加说明]\n" + self.annotations if self.annotations else "")
        return json.dumps(metadata, ensure_ascii=False) + "\n" + body

    @classmethod
    def failure(cls, code: str, text: str, *, error_details: dict | None = None, recovery: str | None = None) -> "ToolResult":
        return cls(text=text, status="error", error_code=code, error_details=error_details, recovery=recovery)


# ---------------------------------------------------------------------------
# 内置工具中文标题映射
# ---------------------------------------------------------------------------

TOOL_TITLES: dict[str, str] = {
    # shell
    "exec_command": "执行命令",
    "write_stdin": "进程输入输出",
    "apply_patch": "应用补丁",
    "submit_plan": "提交计划",
    # 文件工具
    "read_file": "读取文件",
    # 网络工具
    "web_fetch": "获取网页",
    "web_search": "搜索网页",
    # 子 agent
    "task_delegator": "委派任务",
    "load_skill": "加载技能",
    "ask_user": "询问用户",
    "save_memory": "保存记忆",
    "read_memory": "读取记忆",
    "task_create": "创建任务",
    "task_update": "更新任务",
    "task_get": "获取任务",
    "task_list": "任务列表",
    # 工具类
    "compact": "压缩上下文",
    # 实用工具
}


def tool_title(tool_name: str) -> str:
    """返回工具中文标题；未命中映射时返回 '调用 {tool_name}'。"""
    return TOOL_TITLES.get(tool_name, f"调用 {tool_name}")


# ---------------------------------------------------------------------------
# 权限提示
# ---------------------------------------------------------------------------

# AuthorizationResult.source → 中文标签；未登记的来源原样显示（暴露漏配），不回退成「智能权限」。
PERMISSION_SOURCES: dict[str, str] = {
    "hard_rule": "硬规则",
    "plan": "计划模式",
    "policy": "策略放行",
    "judge": "智能权限",
    "web_safety": "网页安全",
    "user": "用户",
    "failure": "授权失败",
}

_PERMISSION_STATUS: dict[str, tuple[str, str]] = {
    "allow": ("✔", "已放行"),
    "deny": ("✘", "已拒绝"),
    "ask": ("?", "需确认"),
}


def permission_line(
    status: Literal["allow", "deny", "ask"],
    tool_name: str,
    reason: str = "",
    decision_source: str = "judge",
) -> str:
    """组装权限提示一行：`{标记} {来源} · {中文工具名} · {结论}({理由})`。

    理由完整保留、不截断，由 UI 容器自动折行；来源取 AuthorizationResult.source，
    空值按 judge 处理；reason 为空时省略括号。
    """
    mark, verdict = _PERMISSION_STATUS[status]
    label = PERMISSION_SOURCES.get(decision_source or "judge", decision_source)
    line = f"{mark} {label} · {tool_title(tool_name)} · {verdict}"
    reason = reason.strip()
    return f"{line}({reason})" if reason else line


# ---------------------------------------------------------------------------
# 参数格式化
# ---------------------------------------------------------------------------

# 命令参数摘要
def _shell_summary(args: dict[str, Any]) -> str:
    """提取 shell 工具的命令摘要。"""
    cmd = args.get("cmd", "")
    lines = cmd.strip().splitlines()
    if len(lines) <= 3:
        return cmd.strip()
    return "\n".join(lines[:3]) + f"\n… 共 {len(lines)} 行"


def format_params(tool_name: str, args: dict[str, Any],
                  budget_lines: int = 20, budget_bytes: int = 4096) -> str:
    """格式化工具参数为展示文本。

    已知内置工具按自然语言摘要，未知/MCP 工具输出格式化 JSON。
    """
    if tool_name == "exec_command":
        return _shell_summary(args)

    if tool_name == "read_file":
        return args.get("path", "")

    if tool_name == "apply_patch":
        return _truncate_text(args.get("patch", ""), budget_lines, budget_bytes)
    if tool_name == "write_stdin":
        return args.get("session_id", "")
    if tool_name == "submit_plan":
        return args.get("title", "")

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

    # 记忆工具
    if tool_name == "save_memory":
        return args.get("title", "")

    if tool_name == "read_memory":
        return args.get("title", "")

    if tool_name == "compact":
        focus = args.get("focus", "")
        if len(focus) > 80:
            focus = focus[:80] + "…"
        return focus


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
        result += "\n..."

    return result, truncated
