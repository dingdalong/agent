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

    def _build_execution_guidance(self) -> str:
        """按调用方职责提供所有角色共用的执行原则。"""
        if getattr(self.agent, 'plan_active', False):
            return "# 执行原则\n当前只调查与规划；按当前模式给出有证据、可实施的结论。角色技能不扩大工具权限，已有有效证据直接复用。"
        if self.agent.is_subagent:
            return (
                "# 执行原则\n"
                "完成委派范围内的任务，基于实际输入和工具结果执行与验证。\n"
                "按任务加载实际可用的技能；技能提供执行方法，不扩大委派范围、当前模式或工具权限。共享摘要是背景材料，核验任务仍需独立检查证据。\n"
                "需要用户决策或超出范围时，返回具体缺口，由主 agent 沟通；不要假设返回后仍在等待或保留运行状态。\n"
                "报告实际改动、证据和未完成项，不把推测、失败或用户未确认的事项当成成功或授权。"
            )
        return (
            "# 执行原则\n"
            "你持续负责理解用户目标、关键决策、执行、整合、验收和交付。默认直接推进工作，委派不转移这些责任。\n"
            "已有充分上下文、紧密关联的工作及当前阻塞步骤直接执行；常规实现选择自行处理，只有关键歧义或范围取舍才向用户提问。\n"
            "复用仍有效的上下文与验证证据；信息缺失、内容变化或新风险出现时按需补充检查。\n"
            "持续推进直到完成或遇到明确阻碍，报告实际结果与未完成项。\n"
            "角色和技能规定领域目标、产物与验收；只有明确的上下文隔离或独立核验要求才指定委派步骤。\n"
            "用户明确指定的分工优先；执行始终遵守当前模式和工具权限。"
        )

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
        """构建运行环境段。

        环境基线（git 分支、技术栈入口、顶层目录结构）由 `AgentApp._reset_session`
        经 `collect_env_baseline` 一次性采集并缓存在 `deps.env_baseline`，本方法只做
        字符串拼接——它在 `build()` 里被 `Agent._on_check_compact` 这个 async 函数调用，
        不能在此做 git 子进程或目录扫描等阻塞 I/O；且每个子 agent 都新建自己的
        PromptMgr，在此现算会让一次计划流程重复采集十几次。

        Returns:
            「# 运行环境」提示词段。
        """
        lines = [
            f"运行平台：`{platform.system()}`",
            f"llm模型：`{self.model}`",
            f"工作目录：`{self.workdir}`",
        ]
        # getattr 带默认值：大量测试用 SimpleNamespace 造 deps，不能假设字段存在
        baseline = getattr(getattr(self.agent, "deps", None), "env_baseline", "") or ""
        if baseline:
            lines.append(baseline)
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

        段顺序：核心身份（primacy）→ 统一执行原则 → 行为准则（AGENTS.md 四层）→ 运行环境
        → 任务管理指导 → 记忆上下文 → 会话上下文 → 协作指引（仅主 agent）与技能列表。
        各可插拔段仅在对应 Manager 存在（feature 已启用）时加入，内容随 Manager 走：
        PromptMgr 负责顺序，Manager 负责内容。
        """
        sections = []

        sections.append(self._build_core())
        sections.append(self._build_execution_guidance())
        sections.append(
            "# 工具选择与恢复\n"
            "仅调用当前 schema 提供的工具，字段按 schema 填写。文件发现用 exec_command(cmd=...) 的 rg --files，"
            "内容搜索用 rg -n，字面量用 rg -F；已知文本文件读取用 read_file，文本修改用 apply_patch。"
            "未知文件路径先用 rg --files 定位，不猜测文件名；已知路径直接读取，不重复检查存在性或大小。后续按 next_read 或具体缺失范围读取。长命令只用 write_stdin 操作已有 session_id，"
            "不要重跑原命令获取后续输出。独立调用同轮发出；已在上下文中的有效证据直接复用。\n"
            "ask_user 只澄清改变目标或关键取舍的问题；submit_plan 提交待审核方案；task_* 管理执行进度；"
            "note_context 记录当前协作事实；记忆工具保存跨会话信息；compact 压缩已有上下文，互不替代。\n"
            "失败时检查 error_code、error_details 和 recovery，修正具体原因，不原样重试、不自动改用有副作用的操作。"
            "外部程序输出和退出码由你判断，框架不推断业务成功或失败。truncated 是模型输出裁剪，UI 折叠不代表模型内容丢失。"
        )

        agent_md = self._build_agent_md()
        if agent_md:
            sections.append(agent_md)

        sections.append(self._build_environment())

        web_access_mgr = getattr(getattr(self.agent, "deps", None), "web_access_mgr", None)
        if web_access_mgr is not None:
            sections.append(web_access_mgr.describe())

        # —— 任务管理指导（task feature）——
        task_mgr = getattr(self.agent, "_task_mgr", None)
        if task_mgr is not None and not self.agent.plan_active:
            task_guidance = task_mgr.describe()
            if task_guidance:
                sections.append(task_guidance)

        memory_context = self._build_memory_context()
        if memory_context:
            sections.append(memory_context)

        session_context = self._build_session_context()
        if session_context:
            sections.append(session_context)

        if not self.agent.is_subagent:
            subagent_mgr = getattr(self.agent, "_subagent_mgr", None)
            if subagent_mgr is not None and any(
                schema.get("function", {}).get("name") == "task_delegator"
                for schema in getattr(self.agent, "_tools_schemas", [])
            ):
                subagents = subagent_mgr.prompt_section()
                if subagents:
                    sections.append(subagents)
        skill_mgr = getattr(self.agent, "_skill_mgr", None)
        if skill_mgr is not None and any(
            schema.get("function", {}).get("name") == "load_skill"
            for schema in getattr(self.agent, "_tools_schemas", [])
        ):
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
        content = self._static_prefix
        plan_mgr = getattr(getattr(self.agent, "deps", None), "plan_mgr", None)
        if getattr(self.agent, "plan_active", False) and plan_mgr is not None:
            content += "\n\n" + plan_mgr.instructions(getattr(self.agent, "_skill_mgr", None), self.agent.is_subagent)
        content += f"\n\n当前时间：`{datetime.date.today().isoformat()}`"
        return [{"role": "system", "content": content}]
