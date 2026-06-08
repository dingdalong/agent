from __future__ import annotations
from typing import TYPE_CHECKING

import datetime
import os
from pathlib import Path
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.agent import Agent

@dataclass
class PromptMgr:
    agent: Agent
    model: str
    workdir: Path
    role_prompt: str | None = None
    _static_prefix: str | None = field(init=False, default=None)

    def _build_core(self) -> str:
        return (
            "# 核心身份\n"
            "你是一个超级智能体。\n"
            "你的任务是理解用户需求，基于可用上下文和工具给出可靠结果。\n"
            "在做假设之前务必先验证。不要凭空猜测行事。"
        )

    def _build_truthfulness_constraints(self) -> str:
        return (
            "# 真实性和参数约束\n"
            "所有推理、行动和输出只能来自用户输入、已读取上下文、工具返回结果或系统提示中的明确事实。\n"
            "不得为了推进流程而编造任何事实、参数、上下文、结论或用户意图。\n"
            "未知信息必须标为未知或未提供；不要把推测写成事实。\n"
            "缺失信息会影响下一步能否可靠执行时，先向用户提问；"
            "不影响执行时，将缺失信息作为限制或不确定点明确传递。"
        )

    def _build_context_and_tool_constraints(self) -> str:
        return (
            "# 上下文和工具调用约束\n"
            "1. 已读取的文件、计划、模板和工具结果，在当前上下文仍可见且未被修改时必须复用；"
            "不要为确认、重新理解或满足流程措辞重复读取。\n"
            "2. 每次工具调用必须服务当前下一步；不要为“准备后续使用”“顺便看看”或未来阶段预取信息。\n"
            "3. 多阶段流程只获取当前阶段立即需要的信息；后续阶段的信息等进入该阶段后再获取。\n"
            "4. 只有工具返回内容被截断、需要未读取范围、文件可能已修改，或当前上下文缺少完成下一步所需的具体内容时，"
            "才允许重新读取；重读前先说明缺失信息或原因。"
        )

    def _build_skill_listing(self) -> str:
        describe = self.agent._skill_mgr.describe()
        if not describe:
            return ""
        return (
            "# 可用技能\n" + describe +
            "\n\n## 技能使用流程\n"
            "技能用于加载专门流程或知识。\n"
            "1. 任何任务（哪怕只有 1% 可能性匹配某个技能）都必须先调用load_skill加载对应技能，再执行其他操作。\n"
            "2. 如果已加载技能要求委派，委派 prompt 必须同时满足该技能指令和本文的运行决策顺序。"
            "4. 如果技能要求使用 prompt 模板委派子智能体，按“上下文和工具调用约束”读取当前阶段所需模板，"
            "组装完整 prompt 后使用 `task_delegator` 委派。"
        )

    def _build_subagent_listing(self) -> str:
        describe = self.agent._subagent_mgr.describe()
        if not describe:
            return ""
        return "# 可用子智能体\n" + describe

    def _build_controller_decision_order(self) -> str:
        describe = self.agent._subagent_mgr.describe()
        if not describe:
            return ""
        return (
            "# 运行决策顺序\n"
            "你是总控 agent，核心职责是理解用户目标、拆分任务、委派子 agent、整合结果并向用户交付。\n"
            "按以下顺序处理每个用户请求：\n"
            "1. 先判断用户需求是否足够明确；如果关键信息缺失，先向用户提问。\n"
            "2. 根据可用子智能体列表判断谁适合承担任务。只要某个子 agent 的职责描述匹配任务的一部分，"
            "并且该部分可以被清晰交代为独立子任务，就必须先调用 `task_delegator` 委托合适的子 agent 完成；"
            "不要默认自己直接调用业务工具。\n"
            "3. 没有合适的子 agent、子 agent 结果不足且只需少量补充验证，或正在做最终整合和回复时，才直接调用其他工具。\n"
            "4. 子 agent 返回后，判断是否需要继续委派、补充确认或直接整合回复。\n\n"
            "跳过委派是例外，不是自由权衡项。只有明确命中上面第 1 或第 3 条，"
            "才能跳过 `task_delegator`。不要基于便利性、已有上下文或主观效率判断跳过委派；"
            "匹配子 agent 时，先委派，再整合。\n\n"
            "不要把策略绑定到某一种固定任务类型；是否委派只取决于子 agent 的职责描述和当前任务边界。\n"
            "委派时不要把用户原始需求原封不动转交。请给子 agent 一个边界清楚的小任务 prompt，说明目标、"
            "相关上下文、期望输出和限制。子 agent 返回后，你负责判断是否继续委派、是否需要补充确认，以及如何回复用户。"
        )

    def _build_role_prompt(self) -> str:
        if not self.role_prompt:
            return ""
        return f"# 角色提示词：\n{self.role_prompt}"

    def _build_agent_md(self) -> str:
        """三层加载 AGENT.md：内置 → 用户全局 → 项目级，内容叠加。

        Returns:
            拼接后的 AGENT.md 提示词段落；无任何来源时返回空字符串。
        """
        from src.mgr.paths import builtin_root

        sources = []

        builtin_agent = builtin_root() / "AGENT.md"
        if builtin_agent.exists():
            sources.append(("builtin (src/AGENT.md)", builtin_agent.read_text()))

        user_agent = Path.home() / ".Agent" / "AGENT.md"
        if user_agent.exists():
            sources.append(("user global (~/.Agent/AGENT.md)", user_agent.read_text()))

        project_agent = self.workdir / "AGENT.md"
        if project_agent.exists():
            sources.append(("project root (AGENT.md)", project_agent.read_text()))

        if not sources:
            return ""
        parts = [
            "# AGENT.md instructions",
            "AGENT.md 可以补充行为要求、项目约定和用户偏好；绝对不能覆盖工具权限、子 agent 隔离和运行决策顺序。",
        ]
        for label, content in sources:
            parts.append(f"## From {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _build_environment(self) -> str:
        lines = [
            f"运行平台：`{os.uname().sysname}`",
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

        段顺序经过设计：
        - 通用行为规则在开头（primacy），参考/上下文材料在中间，
          智能体专属关键指令在结尾（recency）。
        - 共享段集中在前部，分歧段推到尾部，以最大化 prompt cache 前缀命中。
        新增段时须判断是否适用于子智能体：
        - 仅主 agent 使用的段放入 is_subagent 守卫内。
        - 通用段放在守卫外。
        """
        sections = []

        # —— 通用行为规则（开头，primacy）——
        sections.append(self._build_core())
        sections.append(self._build_truthfulness_constraints())
        sections.append(self._build_context_and_tool_constraints())

        # —— 共享参考/上下文材料（中间）——
        agent_md = self._build_agent_md()
        if agent_md:
            sections.append(agent_md)

        sections.append(self._build_environment())

        memory_context = self._build_memory_context()
        if memory_context:
            sections.append(memory_context)

        session_context = self._build_session_context()
        if session_context:
            sections.append(session_context)

        # —— 智能体专属指令（结尾，recency）——
        role_prompt = self._build_role_prompt()
        if role_prompt:
            sections.append(role_prompt)

        # 仅主 agent：先列参考数据，最后放最关键的决策规则
        if not self.agent.is_subagent:
            subagents = self._build_subagent_listing()
            if subagents:
                sections.append(subagents)
            skills = self._build_skill_listing()
            if skills:
                sections.append(skills)
            decision_order = self._build_controller_decision_order()
            if decision_order:
                sections.append(decision_order)

        return "\n\n".join(s for s in sections if s)

    def build(self) -> list:
        """构建 system prompt。

        Returns:
            包含单条 system 消息的列表。
        """
        if self._static_prefix is None:
            self._static_prefix = self._build_static_prefix()
        content = self._static_prefix + f"\n\n当前时间：`{datetime.date.today().isoformat()}`"
        return [{"role": "system", "content": content}]
