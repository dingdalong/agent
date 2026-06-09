"""计划文件管理器 — 计划模式切换、计划文件路径生成、读取及 plan 模式指令注入。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.mgr.permission_mgr import PermissionManager
    from src.mgr.reminder_mgr import ReminderMgr

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

    指令注入通过两条路径（由 ReminderMgr 统一调度）：
    - get_turn_start_reminder()：每次 agent.run() 开始时调用，prepend 到用户输入。
    - notify_tool_round() + pop_post_round_reminder()：轮中模式切换或周期性提醒。

    Attributes:
        workdir: workspace 根目录。
        _plan_dir: 计划文件目录，workdir / ".agent" / "plans"。
        _full_instructions_sent: 是否已发送过完整指令。
        _pending_injection: 轮中进入 plan 模式后置 True，下次 pop_post_round_reminder() 消费。
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

    def enter_mode(self, permission_mgr: PermissionManager, reminder_mgr: ReminderMgr) -> bool:
        """进入计划模式的统一入口。切换权限模式、重置注入状态并注册提醒。

        三条入口（enter_plan_mode 工具、/plan 命令、/mode 命令）均应调用此方法。

        Args:
            permission_mgr: 权限管理器，用于切换模式。
            reminder_mgr: 提醒管理器，用于注册 plan 提醒源。

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

        reminder_mgr.register(self)
        return True

    def exit_mode(self, permission_mgr: PermissionManager, reminder_mgr: ReminderMgr) -> bool:
        """退出计划模式的统一出口。恢复权限模式、重置注入状态并注销提醒。

        所有退出 plan 模式的路径（exit_plan_mode 工具、/mode 命令）均应调用此方法。

        Args:
            permission_mgr: 权限管理器，用于恢复进入前的模式。
            reminder_mgr: 提醒管理器，用于注销 plan 提醒源。

        Returns:
            是否成功退出（不在 plan 模式时返回 False）。
        """
        if not self._is_plan_mode(permission_mgr):
            return False

        permission_mgr.restore_pre_plan_mode()

        self._full_instructions_sent = False
        self._pending_injection = False
        self._rounds_since_injection = 0

        reminder_mgr.unregister(self)
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

        首次调用返回完整指令（含基础限制和技能加载指令），后续调用返回简短提醒。
        具体计划工作流由 builtin:plan-workflow 技能提供，不在此处硬编码。

        Returns:
            plan 模式指令字符串。
        """
        if self._full_instructions_sent:
            return (
                "# 计划模式\n"
                "当前处于计划模式。仅允许只读操作和 plan 文件工具。\n"
                "按照已加载的计划工作流技能执行。如果尚未加载，调用 load_skill('builtin:plan-workflow')。"
            )

        self._full_instructions_sent = True
        return (
            "# 计划模式\n"
            "当前处于计划模式。此指令覆盖其他指令中与之冲突的部分。\n\n"
            "## 限制\n"
            "- 禁止编辑、创建或删除计划文件以外的任何项目文件\n"
            "- 禁止执行会修改系统状态的 shell 命令\n"
            "- 允许使用只读工具（读取文件、搜索、浏览等）进行探索\n\n"
            "## 下一步\n"
            "调用 load_skill('builtin:plan-workflow') 加载默认计划工作流，除非用户指定加载其他计划类工作流技能。\n"
            "加载技能后严格按照其指令执行。\n"
            "注意：同时只加载一种影响计划工作流的技能。\n"
        )

    def get_turn_start_reminder(self, permission_mgr: PermissionManager | None) -> str:
        """在 agent.run() 开始时由 ReminderMgr 调用，返回 prepend 到用户输入的 plan 指令。

        非 plan 模式或 permission_mgr 为 None 时返回空串。调用后重置轮次计数。

        Args:
            permission_mgr: 权限管理器，用于判断当前模式。可为 None。

        Returns:
            plan 模式指令字符串，非 plan 模式返回空串。
        """
        if permission_mgr is None or not self._is_plan_mode(permission_mgr):
            return ""
        self._rounds_since_injection = 0
        self._pending_injection = False
        return self._generate_instructions()

    def notify_tool_round(self, tool_names: list[str]) -> None:
        """工具执行轮结束时由 ReminderMgr 调用，累积距离上次注入的轮数。

        Args:
            tool_names: 本轮调用的工具名列表（PlanMgr 不使用，仅累计轮次）。
        """
        self._rounds_since_injection += 1

    def pop_post_round_reminder(self, permission_mgr: PermissionManager | None) -> str | None:
        """POST_ROUND 时由 ReminderMgr 调用，返回 plan 模式指令纯文本。

        触发条件（按优先级）：
        1. _pending_injection 为 True（轮中 enter_plan_mode 触发）
        2. _rounds_since_injection 超过阈值（周期性提醒）
        无需注入时返回 None。标签包装由 ReminderMgr 统一处理。

        Args:
            permission_mgr: 权限管理器，用于判断当前模式。可为 None。

        Returns:
            plan 模式指令纯文本，或 None 表示无需注入。
        """
        if permission_mgr is None or not self._is_plan_mode(permission_mgr):
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
