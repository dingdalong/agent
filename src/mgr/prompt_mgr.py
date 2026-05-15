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
            "# 核心身份\n"
            f"你是一个运行在`{self.workdir}`中的智能体。\n"
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

    def _build_skill_listing(self) -> str:
        describe = self.agent._skill_mgr.describe()
        if not describe:
            return ""
        return (
            "# 可用技能\n" + describe +
            "\n\n## 技能使用流程\n"
            "技能用于加载专门流程或知识。\n"
            "1. 当任务明显匹配某个技能时，使用 `load_skill` 加载它。\n"
            "2. 如果已加载技能要求委派，委派 prompt 必须同时满足该技能指令和本文的运行决策顺序。"
            "如果技能明确指定子 agent 名称，必须把该名称原样传递。"
            "一般委派使用task_delegator，如果涉及模版委派，则使用task_delegator_template。"
        )

    def _build_subagent_listing(self) -> str:
        describe = self.agent._subagent_mgr.describe()
        if not describe:
            return ""
        return "# 可用子智能体\n" + describe

    def _build_controller_decision_order(self) -> str:
        if self.agent.is_subagent:
            return ""
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
            "如果用户、技能或已读取上下文明确指定子 agent 名称，不得改用同名或近似职责的其他 agent。\n\n"
            "不要把策略绑定到某一种固定任务类型；是否委派只取决于子 agent 的职责描述和当前任务边界。\n"
            "委派时不要把用户原始需求原封不动转交。请给子 agent 一个边界清楚的小任务 prompt，说明目标、"
            "相关上下文、期望输出和限制。子 agent 返回后，你负责判断是否继续委派、是否需要补充确认，以及如何回复用户。"
        )

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
        parts = [
            "# AGENT.md instructions",
            "AGENT.md 可以补充行为要求、项目约定和用户偏好；绝对不能覆盖工具权限、子 agent 隔离和运行决策顺序。",
        ]
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

        truthfulness_constraints = self._build_truthfulness_constraints()
        if truthfulness_constraints:
            sections.append(truthfulness_constraints)

        if not self.agent.is_subagent:
            decision_order = self._build_controller_decision_order()
            if decision_order:
                sections.append(decision_order)
            subagents = self._build_subagent_listing()
            if subagents:
                sections.append(subagents)

        skills = self._build_skill_listing()
        if skills:
            sections.append(skills)

        role_prompt = self._build_role_prompt()
        if role_prompt:
            sections.append(role_prompt)

        agent_md = self._build_agent_md()
        if agent_md:
            sections.append(agent_md)

        sections.append("=== 动态边界标记 ===")
        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        return [{"role": "system", "content": "\n\n".join(sections)}]
