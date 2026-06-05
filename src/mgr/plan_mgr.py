"""计划文件管理器 — 计划模式切换、计划文件路径生成、读取及 plan 模式指令注入。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.mgr.permission_mgr import PermissionManager

logger = logging.getLogger(__name__)

# 距离上次注入多少轮后自动补充简短提醒
_REMINDER_INTERVAL = 5

# 计划文件相对于 workdir 的子路径
PLANS_SUBDIR = os.path.join(".agent", "plans")


def is_path_under_plan_dir(file_path: str, workdir: str) -> bool:
    """检查文件路径是否位于 workdir 下的计划目录中。

    Args:
        file_path: 要检查的文件路径字符串。
        workdir: workspace 根目录路径字符串。

    Returns:
        True 表示文件在 plans 目录下，False 表示不在。
    """
    abs_target = os.path.realpath(file_path)
    abs_plan_dir = os.path.realpath(os.path.join(workdir, PLANS_SUBDIR))
    return abs_target.startswith(abs_plan_dir + os.sep)


@dataclass
class PlanMgr:
    """管理计划模式切换、计划文件路径和 plan 模式指令注入。

    计划文件存放在 workdir/.agent/plans/ 目录下，
    文件名由 LLM 在调用 plan_write_file 时根据计划内容命名。

    指令注入通过两条路径：
    - build_instructions()：每次 agent.run() 开始时调用，prepend 到用户输入。
    - notify_round() + pop_pending_message()：轮中模式切换或周期性提醒。

    Attributes:
        workdir: workspace 根目录。
        _plan_dir: 计划文件目录，workdir / ".agent" / "plans"。
        _full_instructions_sent: 是否已发送过完整指令。
        _pending_injection: 轮中进入 plan 模式后置 True，下次 pop_pending_message() 消费。
        _rounds_since_injection: 距离上次指令注入的工具执行轮数。
    """

    workdir: Path
    _plan_dir: Path = field(init=False)
    _full_instructions_sent: bool = field(init=False, default=False)
    _pending_injection: bool = field(init=False, default=False)
    _rounds_since_injection: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._plan_dir = self.workdir / ".agent" / "plans"

    # ── 模式切换 ──────────────────────────────────────────────────────

    def enter_mode(self, permission_mgr: PermissionManager) -> bool:
        """进入计划模式的统一入口。切换权限模式并重置注入状态。

        三条入口（enter_plan_mode 工具、/plan 命令、Shift+Tab）均应调用此方法。

        Args:
            permission_mgr: 权限管理器，用于切换模式。

        Returns:
            是否成功进入（已在 plan 模式时返回 False）。
        """
        if self._is_plan_mode(permission_mgr):
            return False

        from src.mgr.permission_mgr import PLAN_MODE
        permission_mgr.set_mode(PLAN_MODE)

        self._full_instructions_sent = False
        self._pending_injection = True
        self._rounds_since_injection = 0
        return True

    # ── 计划文件路径 ──────────────────────────────────────────────────

    def resolve_plan_path(self, name: str) -> str:
        """根据计划名生成计划文件路径并记录为当前计划。

        由 plan_write_file 在首次写入时调用，LLM 根据计划内容命名。

        Args:
            name: 计划名（如 'fix-auth-bug'），用于生成文件名。

        Returns:
            计划文件的绝对路径字符串。
        """
        self._plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = self._plan_dir / f"{name}.md"
        logger.info("计划文件路径：%s", plan_path)
        return str(plan_path)

    def get_plan_dir(self) -> str:
        """获取计划文件目录路径，供权限检查使用。

        Returns:
            计划文件目录的绝对路径字符串。
        """
        return str(self._plan_dir)

    def is_plan_file(self, file_path: str) -> bool:
        """检查文件路径是否位于计划目录下。

        Args:
            file_path: 要检查的文件绝对路径字符串。

        Returns:
            True 表示文件在 plans 目录下，False 表示不在。
        """
        return is_path_under_plan_dir(file_path, str(self.workdir))

    # ── 指令注入 ──────────────────────────────────────────────────────

    def _is_plan_mode(self, permission_mgr: PermissionManager) -> bool:
        """检查当前是否处于 plan 模式。

        Args:
            permission_mgr: 权限管理器实例。

        Returns:
            True 表示处于 plan 模式。
        """
        from src.mgr.permission_mgr import PLAN_MODE
        return permission_mgr.mode is PLAN_MODE

    def _generate_instructions(self) -> str:
        """生成 plan 模式指令文本（完整或简短）。

        首次调用返回完整指令（含限制和工作流），后续调用返回简短提醒。

        Returns:
            plan 模式指令字符串。
        """
        if self._full_instructions_sent:
            return (
                "# 计划模式\n"
                "当前处于计划模式。仅允许只读操作和 plan 文件工具。\n"
                "必须按顺序执行：充分探索 → 与用户沟通需求 → plan_write_file 写入计划 → exit_plan_mode 提交审核。不得跳过任何步骤。"
            )

        self._full_instructions_sent = True
        return (
            "# 计划模式\n"
            "当前处于计划模式。无论任务多么简单，你都必须严格遵循完整的计划工作流，不得跳过任何步骤。\n\n"
            "## 限制\n"
            "- 禁止编辑、创建或删除计划文件以外的任何项目文件\n"
            "- 禁止执行会修改系统状态的 shell 命令\n"
            "- 允许使用只读工具（读取文件、搜索、浏览等）进行探索\n\n"
            "## 工作流程（必须按顺序执行，不得跳过）\n"
            "1. **充分探索**：使用只读工具充分了解任务相关的现有内容和上下文。即使任务看似简单，也必须先探索确认理解正确\n"
            "2. **沟通需求**：向用户确认需求细节、澄清不明确之处，收集到必要信息后再进入下一步\n"
            "3. **撰写计划**：调用 plan_write_file 写入结构化计划文件，计划必须包含以下章节：\n"
            "   - **背景**：任务目标和当前代码现状\n"
            "   - **实现步骤**：每个步骤说明要修改什么、如何修改\n"
            "   - **涉及文件**：列出所有需要新增或修改的文件\n"
            "   - **验证方式**：如何确认实现正确（测试、手动验证等）\n"
            "4. **提交审核**：调用 exit_plan_mode 提交计划供用户审核\n"
            "5. 若用户要求修改，回到步骤 2 重新沟通需求、修改计划并再次提交审核\n\n"
            "## 禁止行为\n"
            "- 禁止不经探索直接写计划\n"
            "- 禁止未与用户确认需求就直接撰写计划\n"
            "- 禁止不写计划文件直接调用 exit_plan_mode\n"
            "- 禁止以「任务简单」为由跳过任何步骤"
        )

    def build_instructions(self, permission_mgr: PermissionManager) -> str:
        """在 agent.run() 开始时调用，返回 prepend 到用户输入的 plan 指令。

        非 plan 模式返回空串。调用后重置轮次计数。

        Args:
            permission_mgr: 权限管理器，用于判断当前模式。

        Returns:
            plan 模式指令字符串，非 plan 模式返回空串。
        """
        if not self._is_plan_mode(permission_mgr):
            return ""
        self._rounds_since_injection = 0
        self._pending_injection = False
        return self._generate_instructions()

    def notify_round(self) -> None:
        """在 _on_execute_tools() 中调用，累积距离上次注入的轮数。"""
        self._rounds_since_injection += 1

    def pop_pending_message(self, permission_mgr: PermissionManager) -> str | None:
        """在 _on_post_round() 中调用，返回需要追加到 messages 的指令内容。

        触发条件（按优先级）：
        1. _pending_injection 为 True（轮中 enter_plan_mode 触发）
        2. _rounds_since_injection 超过阈值（周期性提醒）
        无需注入时返回 None。

        Args:
            permission_mgr: 权限管理器，用于判断当前模式。

        Returns:
            指令字符串或 None。
        """
        if not self._is_plan_mode(permission_mgr):
            return None

        if self._pending_injection:
            self._pending_injection = False
            self._rounds_since_injection = 0
            return self._generate_instructions()

        if self._rounds_since_injection >= _REMINDER_INTERVAL:
            self._rounds_since_injection = 0
            return self._generate_instructions()

        return None

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self._full_instructions_sent = False
        self._pending_injection = False
        self._rounds_since_injection = 0
