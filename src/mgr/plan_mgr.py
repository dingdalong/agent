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

from src.mode import RunMode, plan_mode_boundary

if TYPE_CHECKING:
    from src.agent import Agent

@dataclass
class PlanMgr:
    """管理模式及计划持久化；当前模式正文由 PromptMgr 组装。"""

    workdir: Path
    _plan_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self._plan_dir = self.workdir / ".agent" / "plans"

    # ── 模式切换 ──────────────────────────────────────────────────────

    def enter_mode(self, agent: Agent) -> bool:
        """切换到计划模式；schema 与稳定提示前缀保持不变。"""
        if agent.mode is RunMode.PLAN:
            return False
        agent.mode = RunMode.PLAN
        return True

    def exit_mode(self, agent: Agent) -> bool:
        """切换到普通模式；schema 与稳定提示前缀保持不变。"""
        if agent.mode is RunMode.EXECUTE:
            return False
        agent.mode = RunMode.EXECUTE
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

    def instructions(self, is_subagent: bool) -> str:
        """返回计划模式的完整框架指令。"""
        boundary = plan_mode_boundary(can_submit_plan=not is_subagent) + "\n\n"
        if is_subagent:
            return (
                boundary
                + "# 规划职责\n"
                "只调查并回答委派的具体问题，提供证据、影响和未决风险；不提交完整计划。"
            )
        return (
            boundary
            + "# 规划流程\n"
            "不要创建执行进度任务。先读取仓库规则并定位行为入口、状态权威写入者、现有接口和相关测试；"
            "能从仓库或系统发现的事实自行调查，不向用户提问。首次探索将独立的文件发现、内容搜索和读取合并到同一轮；"
            "只调查与目标有关的路径，复用已有可靠证据，不重复搜索或读取。\n"
            "能够明确陈述目标、成功标准、范围边界和约束后，只对无法从环境推导且会改变方案的关键选择集中询问用户；"
            "普通实现选择遵循仓库惯例自行确定。\n"
            "围绕选定方案补齐接口或 schema、数据流、状态归属、生命周期、关键边界、失败路径、测试与验收。"
            "当这些决策已明确时立即调用 submit_plan(title, content)，不得为了习惯性复查继续调查。\n"
            "计划正文必须自包含，按摘要、关键改动、测试和必要假设组织；涉及关键流程时使用图示。"
            "框架负责保存和审核，不另写计划文件或重复输出正文；收到修改意见后提交完整替代版本。"
        )
