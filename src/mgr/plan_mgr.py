"""计划文件管理器 — 计划模式切换、计划文件路径生成及 plan 模式指令注入。

提示词说明流程，PermissionManager 强制 Plan 权限边界。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent import Agent
    from src.mgr.reminder_mgr import ReminderMgr

# 计划工作流技能键（builtin 命名空间；角色可用同名技能覆盖共享层实现）
_PLAN_SKILL_KEY = "builtin:plan-workflow"


@dataclass
class PlanMgr:
    """管理计划模式切换、计划文件路径和 plan 模式指令注入。

    计划正文经 submit_plan 保存；权限边界由 PermissionManager 强制执行。

    指令注入通过 ReminderMgr 统一调度：
    - get_turn_start_reminder()：每次 turn 开始时调用。
    - pop_post_round_reminder()：轮中进入 plan 模式时注入一次指令。

    Attributes:
        workdir: workspace 根目录。
        _plan_dir: 计划文件目录（workdir / ".agent" / "plans"），内部使用。
        _pending_injection: 轮中进入 plan 模式后置 True，下次 pop_post_round_reminder() 消费。
        _need_exit_reminder: 退出 plan 模式后置 True，下次 turn start 输出一次性退出提醒后清除。
    """

    workdir: Path
    _plan_dir: Path = field(init=False)
    _pending_injection: bool = field(init=False, default=False)
    _need_exit_reminder: bool = field(init=False, default=False)
    _reminder_mgr: ReminderMgr | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._plan_dir = self.workdir / ".agent" / "plans"

    # ── 模式切换 ──────────────────────────────────────────────────────

    def enter_mode(self, agent: Agent, reminder_mgr: ReminderMgr) -> bool:
        """进入计划模式并注册提醒。

        /plan 命令、Shift+Tab 和 startInPlanMode 初始化均调用此方法。

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

    def save(self, content: str, previous: dict) -> Path:
        """只保存到受控目录；模型不提供目标路径。"""
        from src.mgr.path_resolver import PathResolver, PathClass
        resolver = PathResolver(self.workdir)
        directory = resolver.resolve(self._plan_dir)
        if directory != self.workdir.resolve() / ".agent" / "plans":
            raise ValueError("计划目录不能重定向到其他位置")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (Path(previous["path"]).name if previous.get("path") else f"{uuid.uuid4().hex}.md")
        if resolver.classify(resolver.resolve(target)) is not PathClass.PLAN or target.is_symlink():
            raise ValueError("计划目标不是受控普通文件")
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content.rstrip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def _generate_instructions(self, is_subagent: bool) -> str:
        text = (
            "# 计划模式\n禁止实施项目改动。探索使用 exec_command 的受限只读命令或 read_file；"
            "不要运行测试、构建或任意脚本。仅用户或计划审核可切换模式。\n"
        )
        if is_subagent:
            return text + "只回答委派的具体问题，不提交计划。"
        return text + (
            f"加载 {_PLAN_SKILL_KEY}。行为、关键边界和验收已明确即可用 submit_plan(title, content) "
            "一次提交完整计划；框架负责保存与审核，不另写文件或登记路径。"
        )

    def get_turn_start_reminder(self, plan_active: bool, is_subagent: bool) -> str:
        """在 agent.run() 开始时由 ReminderMgr 调用，返回 prepend 到用户输入的提醒。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。
            is_subagent: 调用方是否为子智能体。

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
        return self._generate_instructions(is_subagent)

    def pop_post_round_reminder(self, plan_active: bool, is_subagent: bool) -> str | None:
        """POST_ROUND 时由 ReminderMgr 调用，返回 plan 模式指令纯文本。

        仅在轮中进入 plan 模式时触发（_pending_injection），
        无需注入时返回 None。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。
            is_subagent: 调用方是否为子智能体。

        Returns:
            plan 模式指令纯文本，或 None 表示无需注入。
        """
        if not plan_active:
            return None

        if self._pending_injection:
            self._pending_injection = False
            return self._generate_instructions(is_subagent)

        return None

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self._pending_injection = False
        self._need_exit_reminder = False
