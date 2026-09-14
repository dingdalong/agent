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

# 计划工作流技能键（builtin 命名空间；角色可用同名技能覆盖共享层实现）
_PLAN_SKILL_KEY = "builtin:plan-workflow"


@dataclass
class PlanMgr:
    """管理模式及计划持久化；当前模式正文由 PromptMgr 组装。"""

    workdir: Path
    _plan_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self._plan_dir = self.workdir / ".agent" / "plans"

    # ── 模式切换 ──────────────────────────────────────────────────────

    def enter_mode(self, agent: Agent) -> bool:
        """切换模式并刷新可用工具和提示缓存。"""
        if agent.plan_active:
            return False
        agent.plan_active = True
        agent.refresh_tools_schemas()

        return True

    def exit_mode(self, agent: Agent) -> bool:
        """退出模式并刷新可用工具和提示缓存。"""
        if not agent.plan_active:
            return False
        agent.plan_active = False
        agent.refresh_tools_schemas()

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

    def instructions(self, skill_mgr, is_subagent: bool) -> str:
        """当前模式提示只进入系统段，不累积到用户历史。"""
        boundary = "# 计划模式\n只调查与规划，不实施项目改动；验证仅可写专用临时目录。仅用户或计划审核可切换模式。\n"
        if is_subagent:
            return boundary + "只回答委派的具体问题，不提交计划。"
        if skill_mgr is not None and skill_mgr.check_skill(_PLAN_SKILL_KEY):
            return boundary + skill_mgr.load_full_text(_PLAN_SKILL_KEY)
        return boundary + "先核实环境事实，再确认无法从仓库推导的用户意图，最后补齐接口、数据流、失败路径和验收；决策完整后立即用 submit_plan 一次提交。"
