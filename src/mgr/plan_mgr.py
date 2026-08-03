"""计划文件管理器 — 计划模式切换、计划文件路径生成及 plan 模式指令注入。

通过提示词约束 LLM 行为（只用只读工具、只写计划文件）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent import Agent
    from src.mgr.reminder_mgr import ReminderMgr

logger = logging.getLogger(__name__)

# 计划工作流技能键（builtin 命名空间；角色可用同名技能覆盖共享层实现）
_PLAN_SKILL_KEY = "builtin:plan-workflow"


@dataclass
class PlanMgr:
    """管理计划模式切换、计划文件路径和 plan 模式指令注入。

    通过提示词约束 LLM 在 plan 模式下只使用只读工具和计划文件操作。

    指令注入通过 ReminderMgr 统一调度：
    - get_turn_start_reminder()：每次 turn 开始时调用。
    - pop_post_round_reminder()：轮中进入 plan 模式时注入一次指令。

    Attributes:
        workdir: workspace 根目录。
        _plan_dir: 计划文件目录（workdir / ".agent" / "plans"），内部使用。
        _pending_injection: 轮中进入 plan 模式后置 True，下次 pop_post_round_reminder() 消费。
        _need_exit_reminder: 退出 plan 模式后置 True，下次 turn start 输出一次性退出提醒后清除。
        _active_plan_path: 当前会话正在处理的计划文件路径，在 plan 模式指令中引用。
            写入计划目录时由 FileMgr 自动设置，仅 reload() 时重置。
    """

    workdir: Path
    _plan_dir: Path = field(init=False)
    _pending_injection: bool = field(init=False, default=False)
    _need_exit_reminder: bool = field(init=False, default=False)
    _active_plan_path: str | None = field(init=False, default=None)
    _reminder_mgr: ReminderMgr | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._plan_dir = self.workdir / ".agent" / "plans"

    # ── 模式切换 ──────────────────────────────────────────────────────

    def enter_mode(self, agent: Agent, reminder_mgr: ReminderMgr) -> bool:
        """进入计划模式并注册提醒。

        enter_plan_mode 工具、/plan 命令和 Shift+Tab 均调用此方法。

        Args:
            agent: 目标 Agent。
            reminder_mgr: 提醒管理器，用于注册 plan 提醒源。

        Returns:
            是否成功进入（已在 plan 模式时返回 False）。
        """
        if agent.plan_active:
            return False
        agent.plan_active = True

        self._pending_injection = True
        self._need_exit_reminder = False

        self._reminder_mgr = reminder_mgr
        reminder_mgr.register(self)
        return True

    def exit_mode(self, agent: Agent, reminder_mgr: ReminderMgr) -> bool:
        """退出计划模式并设置退出提醒。

        不立即注销 reminder_mgr，保留一轮用于输出退出提醒。

        Args:
            agent: 目标 Agent。
            reminder_mgr: 提醒管理器，退出提醒输出后才注销。

        Returns:
            是否成功退出（不在 plan 模式时返回 False）。
        """
        if not agent.plan_active:
            return False
        agent.plan_active = False

        self._pending_injection = False
        self._need_exit_reminder = True

        self._reminder_mgr = reminder_mgr
        return True

    # ── 计划文件路径 ──────────────────────────────────────────────────

    def set_active_plan_path(self, path: str) -> None:
        """设置当前活跃计划文件路径。

        由 set_plan_file 工具调用，LLM 在写入计划文件时主动设置。

        Args:
            path: 计划文件的绝对路径。
        """
        self._active_plan_path = path

    # ── 指令注入 ──────────────────────────────────────────────────────

    def _generate_instructions(self) -> str:
        """生成 plan 模式指令文本。

        统一版本，不区分首次/后续。包含完整约束和工作流指引。

        Returns:
            plan 模式指令字符串。
        """
        plan_dir = str(self._plan_dir)
        text = (
            "# 计划模式\n"
            "当前处于计划模式。此指令覆盖其他指令中与之冲突的部分。\n\n"
            "## 限制\n"
            "- 禁止编辑、创建或删除计划文件以外的任何项目文件\n"
            "- 禁止执行会修改系统状态的 shell 命令\n"
            "- 允许使用只读工具（读取文件、搜索、浏览等）进行探索\n"
            f"- 计划文件目录：{plan_dir}\n"
            "- 使用 write_file / edit_file_lines 操作计划文件（路径以上述目录为前缀）\n\n"
            "## 下一步\n"
            f"调用 load_skill('{_PLAN_SKILL_KEY}') 加载计划工作流，然后严格按照其指令执行。\n"
        )

        if self._active_plan_path:
            text += (
                "\n## 当前计划\n"
                f"当前正在处理的计划文件：{self._active_plan_path}\n"
                "继续在此文件上修改和完善计划。"
                "若用户的新请求与当前计划内容差异较大，应创建新的计划文件（路径自动更新）。\n"
            )

        return text

    def get_turn_start_reminder(self, plan_active: bool) -> str:
        """在 agent.run() 开始时由 ReminderMgr 调用，返回 prepend 到用户输入的提醒。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。

        Returns:
            提醒字符串，无需注入时返回空串。
        """
        # 退出 plan 模式后的一次性提醒
        if self._need_exit_reminder and not plan_active:
            self._need_exit_reminder = False
            if self._reminder_mgr is not None:
                self._reminder_mgr.unregister(self)
            plan_dir = str(self._plan_dir)
            return (
                "## 已退出计划模式\n"
                f"你现在可以编辑文件、运行工具和执行操作。计划目录：{plan_dir}"
            )

        if not plan_active:
            return ""

        self._pending_injection = False
        return self._generate_instructions()

    def pop_post_round_reminder(self, plan_active: bool) -> str | None:
        """POST_ROUND 时由 ReminderMgr 调用，返回 plan 模式指令纯文本。

        仅在轮中进入 plan 模式时触发（_pending_injection），
        无需注入时返回 None。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。

        Returns:
            plan 模式指令纯文本，或 None 表示无需注入。
        """
        if not plan_active:
            return None

        if self._pending_injection:
            self._pending_injection = False
            return self._generate_instructions()

        return None

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self._pending_injection = False
        self._need_exit_reminder = False
        self._active_plan_path = None
