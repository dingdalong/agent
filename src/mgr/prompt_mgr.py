from __future__ import annotations
from typing import TYPE_CHECKING

import datetime
import os
from pathlib import Path
from dataclasses import dataclass, field
from collections import deque

if TYPE_CHECKING:
    from src.agent import Agent

@dataclass
class PromptMgr:
    agent: Agent
    model: str
    workdir: Path
    role_prompt: str | None = None
    skills: deque[str] = field(default_factory=lambda: deque(maxlen=2))

    """
    从独立的部分组装系统提示。
    这里的设计原则是清晰性：
    每个部分有单一来源、单一职责。
    这使得提示更易于推理、更易于测试，也更易于随着智能体能力的增长而演进。
    """

    def load_skill(self, name) -> str:
        if not self.agent._skill_mgr.check_skill(name):
            describe = self.agent._skill_mgr.describe() or "无"
            return f"错误: 不存在的技能：'{name}'。可用技能列表：\n" + "\n".join(describe)
        else:
            self.skills.append(name)
            return f"成功加载技能：'{name}'"

    def _build_core(self) -> str:
        return (
            f"你是一个运行在`{self.workdir}`中的智能体。\n"
            "可以使用所提供的工具来完成用户的需求。\n"
            "在做假设之前务必*先验证*。**不要凭空猜测行事**。\n"
        )

    def _build_skill_listing(self) -> str:
        describe = self.agent._skill_mgr.describe()
        if not describe:
            return ""
        else:
            return "# 可用技能列表：\n" + describe

    def _build_subagent_listing(self) -> str:
        describe = self.agent._subagent_mgr.describe()
        if not describe:
            return ""
        else:
            return "# 可用子智能体列表：\n" + describe + "\n **善用子智能体来协助完成任务**"

    def _build_role_prompt(self) -> str:
        if not self.role_prompt:
            return ""
        return f"# 角色提示词：\n{self.role_prompt}"

    def _build_agent_md(self) -> str:
        """
        按优先级顺序加载 AGENT.md 文件（全部包含）：
        ~/.Agent/AGENT.md（用户级全局指令）
        <项目根目录>/AGENT.md（项目级指令）
        <当前子目录>/AGENT.md（目录专属指令）
        """
        sources = []

        user_agent = Path.home() / ".Agent" / "AGENT.md"
        if user_agent.exists():
            sources.append(("user global (~/.Agent/AGENT.md)", user_agent.read_text()))

        project_agent = self.workdir / "AGENT.md"
        if project_agent.exists():
            sources.append(("project root (AGENT.md)", project_agent.read_text()))

        cwd = Path.cwd()
        if cwd != self.workdir:
            subdir_agent = cwd / "AGENT.md"
            if subdir_agent.exists():
                sources.append((f"subdir ({cwd.name}/AGENT.md)", subdir_agent.read_text()))
        if not sources:
            return ""
        parts = ["# AGENT.md instructions"]
        for label, content in sources:
            parts.append(f"## From {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _build_dynamic_context(self) -> str:
        loaded_skills = "\n\n".join(
            self.agent._skill_mgr.load_full_text(name)
            for name in self.skills
        )
        lines = [
            f"运行平台：`{os.uname().sysname}`",
            f"llm模型：`{self.model}`",
            f"工作目录：`{self.workdir}`",
        ]
        ctx = "# 动态上下文\n" + "\n".join(lines)
        if loaded_skills:
            ctx += "\n\n# 已加载技能\n" + loaded_skills
        ctx += f"\n\n当前时间：`{datetime.date.today().isoformat()}`"
        return ctx

    def build(self) -> list:
        """
        将所有部分组装成完整的系统提示。
        静态部分（1-5）与动态部分（6）通过 === 动态边界标记 === 标记分隔。
        在实际的缓存补全（CC）中，静态前缀会在多轮对话中被缓存，以节省提示词令牌。
        """
        sections = []
        core = self._build_core()
        if core:
            sections.append(core)

        role_prompt = self._build_role_prompt()
        if role_prompt:
            sections.append(role_prompt)

        skills = self._build_skill_listing()
        if skills:
            sections.append(skills)

        if not self.agent.is_subagent:
            subagents = self._build_subagent_listing()
            if subagents:
                sections.append(subagents)

        agent_md = self._build_agent_md()
        if agent_md:
            sections.append(agent_md)

        sections.append("=== 动态边界标记 ===")
        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        return [{"role": "system", "content": "\n\n".join(sections)}]
