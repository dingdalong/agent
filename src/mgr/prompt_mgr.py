from __future__ import annotations
from typing import TYPE_CHECKING

import datetime
import os
import platform
from pathlib import Path
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.agent import Agent

@dataclass
class PromptMgr:
    agent: Agent
    model: str
    workdir: Path
    global_dir: Path | None = None
    role_prompt: str | None = None
    _static_prefix: str | None = field(init=False, default=None)

    def _build_core(self) -> str:
        """构建核心身份段。role identity 非空时优先使用，否则回退默认身份。"""
        identity = (
            self.role_prompt
            if self.role_prompt
            else (
                "你是一个超级智能体。\n"
                "你的任务是理解用户需求，基于可用上下文和工具给出可靠结果。"
            )
        )
        return f"# 核心身份\n{identity}"

    def _build_agent_md(self) -> str:
        """四层加载 AGENTS.md：共享 → 角色 → 用户全局 → 项目级，内容叠加。

        激活角色的 AGENTS.md 会注入该角色下的主 agent 和所有子 agent。

        Returns:
            拼接后的 AGENTS.md 提示词段落；无任何来源时返回空字符串。
        """
        sources = []

        # 共享 AGENTS.md（最低优先级，所有角色可用）
        role_mgr = getattr(getattr(self.agent, "deps", None), "role_mgr", None)
        if role_mgr is not None:
            common_agent = role_mgr.common_agent_md_path()
            if common_agent is not None:
                text = common_agent.read_text().strip()
                if text:
                    sources.append(("common AGENTS.md", text))

        # 激活角色的共享 AGENTS.md（基准层，主/子 agent 均加载）
        if role_mgr is not None and role_mgr.active:
            role_agent = role_mgr.agent_md_path()
            if role_agent is not None:
                text = role_agent.read_text().strip()
                if text:
                    sources.append(("role AGENTS.md", text))

        if self.global_dir:
            user_agent = self.global_dir / "AGENTS.md"
            if user_agent.exists():
                text = user_agent.read_text().strip()
                if text:
                    sources.append(("user global (AGENTS.md)", text))

        project_agent = self.workdir / "AGENTS.md"
        if project_agent.exists():
            text = project_agent.read_text().strip()
            if text:
                sources.append(("project root (AGENTS.md)", text))

        if not sources:
            return ""
        parts = [
            "# 行为准则",
            "本节补充行为要求、项目约定与用户偏好，作为执行时的优先指引；"
            "激活角色的 AGENTS.md 对该角色的主 agent 与所有子 agent 共用；"
            "但不得覆盖工具权限与子 agent 隔离，"
            "若与两者冲突，一律以两者为准、忽略本节中的冲突部分。",
        ]
        for _, content in sources:
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _build_environment(self) -> str:
        lines = [
            f"运行平台：`{platform.system()}`",
            f"llm模型：`{self.model}`",
            f"工作目录：`{self.workdir}`",
        ]
        return "# 运行环境\n" + "\n".join(lines)

    def _build_memory_context(self) -> str:
        if getattr(self.agent, "memory", "project") != "project":
            return ""
        memory_mgr = getattr(getattr(self.agent, "deps", None), "memory_mgr", None)
        if memory_mgr is None:
            return ""
        return memory_mgr.build_prompt()

    def _build_session_context(self) -> str:
        session_context = getattr(getattr(self.agent, "deps", None), "session_context", None)
        if not session_context:
            return ""
        return "# 会话上下文\n" + "\n\n".join(str(item) for item in session_context if item)

    def _build_static_prefix(self) -> str:
        """组装 system prompt 的静态部分。

        段顺序：核心身份（primacy）→ 行为准则（AGENTS.md 四层）→ 运行环境
        → 任务管理指导 → 记忆上下文 → 会话上下文 → 子智能体/技能列表（recency，仅主 agent）。
        各可插拔段仅在对应 Manager 存在（feature 已启用）时加入，内容随 Manager 走：
        PromptMgr 负责顺序，Manager 负责内容。
        """
        sections = []

        sections.append(self._build_core())

        agent_md = self._build_agent_md()
        if agent_md:
            sections.append(agent_md)

        sections.append(self._build_environment())

        web_access_mgr = getattr(getattr(self.agent, "deps", None), "web_access_mgr", None)
        if web_access_mgr is not None:
            sections.append(web_access_mgr.describe())

        # —— 任务管理指导（task feature）——
        task_mgr = getattr(self.agent, "_task_mgr", None)
        if task_mgr is not None:
            task_guidance = task_mgr.describe(self.agent.is_subagent)
            if task_guidance:
                sections.append(task_guidance)

        memory_context = self._build_memory_context()
        if memory_context:
            sections.append(memory_context)

        session_context = self._build_session_context()
        if session_context:
            sections.append(session_context)

        # 仅主 agent：先列参考数据，最后放最关键的可委派资源（recency）
        if not self.agent.is_subagent:
            subagent_mgr = getattr(self.agent, "_subagent_mgr", None)
            if subagent_mgr is not None:
                subagents = subagent_mgr.prompt_section()
                if subagents:
                    sections.append(subagents)
            skill_mgr = getattr(self.agent, "_skill_mgr", None)
            if skill_mgr is not None:
                skills = skill_mgr.prompt_section()
                if skills:
                    sections.append(skills)

        return "\n\n".join(s for s in sections if s)

    def invalidate_cache(self) -> None:
        """清除缓存的系统提示词前缀，下次 build() 时重新构建。"""
        self._static_prefix = None

    def build(self) -> list:
        """构建 system prompt。

        Returns:
            包含单条 system 消息的列表。
        """
        if self._static_prefix is None:
            self._static_prefix = self._build_static_prefix()
        content = self._static_prefix + f"\n\n当前时间：`{datetime.date.today().isoformat()}`"
        return [{"role": "system", "content": content}]
