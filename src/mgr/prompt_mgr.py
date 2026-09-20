"""提示词组装 — 只消费显式的 agent 事实，不持有 Agent 实例。

`PromptFacts` 是装配期冻结的窄接口：身份、工具、feature 与所需 Manager
引用。随会话变化的 `deps.session` 由 `PromptMgr` 活引用读取，当前模式则由
调用方在每次组装时显式传入，二者都不经过 `Agent` 本身。
"""

from __future__ import annotations
from typing import TYPE_CHECKING

import datetime
import platform
from pathlib import Path
from dataclasses import dataclass, field

from src.mode import RunMode
from src.common.tool_policy import ToolAudience
from src.prompt_tags import (
    ExternalContextItem,
    PromptConsumer,
    render_external_context,
    render_tag_guide,
)

if TYPE_CHECKING:
    from src.agent import AgentDeps
    from src.mgr.skill_mgr import SkillMgr
    from src.mgr.subagent_mgr import SubAgentMgr
    from src.mgr.task_mgr import TaskManager


@dataclass(frozen=True)
class PromptFacts:
    """PromptMgr 组装提示词所需的 agent 事实。

    Attributes:
        agent_type: 当前 agent 类型标识。
        is_subagent: 是否为子 agent。
        role_prompt: role.md body；为空时回退默认身份。
        tools: 声明的工具白名单；None 表示未声明。
        features: 已解析的有效 feature 集。
        memory: 记忆范围；``project`` 时注入项目记忆。
        task_mgr: 任务管理器；未启用时为 None。
        subagent_mgr: 子 agent 管理器；主 agent 未启用或为子 agent 时为 None。
        skill_mgr: 技能管理器；未启用时为 None。
    """

    agent_type: str = "agent"
    is_subagent: bool = False
    role_prompt: str | None = None
    tools: frozenset[str] | None = None
    features: frozenset[str] = frozenset()
    memory: str | None = None
    task_mgr: TaskManager | None = None
    subagent_mgr: SubAgentMgr | None = None
    skill_mgr: SkillMgr | None = None

@dataclass
class PromptMgr:
    facts: PromptFacts
    deps: AgentDeps
    workdir: Path
    global_dir: Path | None = None
    _system_content: str | None = field(init=False, default=None)

    def _build_core(self) -> str:
        """构建核心身份段。role identity 非空时优先使用，否则回退默认身份。"""
        identity = (
            self.facts.role_prompt
            if self.facts.role_prompt
            else (
                "你是一个超级智能体。\n"
                "你的任务是理解用户需求，基于可用上下文和工具给出可靠结果。"
            )
        )
        return f"# 身份与责任\n{identity}"

    def _build_execution_guidance(self) -> str:
        """按调用方职责提供 Agent 生命周期内固定的通用执行原则。"""
        if self.facts.is_subagent:
            return (
                "# 通用执行原则\n"
                "完成委派范围内的任务，基于实际输入和工具结果执行与验证。\n"
                "需要用户决策或超出范围时，返回具体缺口，由主 agent 沟通；不要假设返回后仍在等待或保留运行状态。\n"
                "报告实际改动、验证证据和未完成项，不把推测、失败或用户未确认的事项当成成功或授权。"
            )
        return (
            "# 通用执行原则\n"
            "你持续负责理解用户目标、关键决策、执行、整合、验收和交付。默认直接推进工作，委派不转移这些责任。\n"
            "基于用户输入、当前上下文和工具结果判断事实；缺失信息只有会改变目标、范围或关键取舍时才向用户确认。\n"
            "定位实际调用链和根因，遵守任务范围与现有约定，保护用户已有改动；验证从受影响范围开始并按风险扩展。\n"
            "持续推进直到完成或遇到明确阻碍，交付实际结果、验证证据和未完成项。"
        )

    def _build_agents_context(self) -> list[ExternalContextItem]:
        """四层加载 AGENTS.md，并保留每层外部来源。

        激活角色的 AGENTS.md 会注入该角色下的主 agent 和所有子 agent。

        Returns:
            按共享、角色、全局、项目顺序排列的外部上下文条目。
        """
        items: list[ExternalContextItem] = []

        # 共享 AGENTS.md（最低优先级，所有角色可用）
        role_mgr = getattr(self.deps, "role_mgr", None)
        if role_mgr is not None:
            common_agent = role_mgr.common_agent_md_path()
            if common_agent is not None:
                text = common_agent.read_text().strip()
                if text:
                    items.append(ExternalContextItem("AGENTS.md", text, "common"))

        # 激活角色的共享 AGENTS.md（基准层，主/子 agent 均加载）
        if role_mgr is not None and role_mgr.active:
            role_agent = role_mgr.agent_md_path()
            if role_agent is not None:
                text = role_agent.read_text().strip()
                if text:
                    items.append(ExternalContextItem("AGENTS.md", text, "role"))

        if self.global_dir:
            user_agent = self.global_dir / "AGENTS.md"
            if user_agent.exists():
                text = user_agent.read_text().strip()
                if text:
                    items.append(ExternalContextItem("AGENTS.md", text, "global"))

        project_agent = self.workdir / "AGENTS.md"
        if project_agent.exists():
            text = project_agent.read_text().strip()
            if text:
                items.append(ExternalContextItem("AGENTS.md", text, "project"))
        return items

    def _build_environment_context(self) -> str:
        """构建作为外部上下文提供的运行环境数据。

        环境基线（git 分支、技术栈入口、顶层目录结构）由 `AgentApp._reset_session`
        经 `collect_env_baseline` 一次性采集并缓存在 `deps.session.env_baseline`，本方法只做
        字符串拼接。它由 `build_initial_context_messages()` 在首次 chat 前调用，不能
        在此做 git 子进程或目录扫描等阻塞 I/O；且每个子 agent 都新建自己的
        PromptMgr，在此现算会让一次协作流程重复采集多次。

        Returns:
            「# 运行环境」上下文段。
        """
        lines = [
            f"运行平台：`{platform.system()}`",
            f"工作目录：`{self.workdir}`",
            f"当前日期：`{datetime.date.today().isoformat()}`",
        ]
        # getattr 带默认值：大量测试用 SimpleNamespace 造 deps，不能假设字段存在
        session = getattr(self.deps, "session", None)
        baseline = getattr(session, "env_baseline", "") or ""
        if baseline:
            lines.append(baseline)
        return "# 运行环境\n" + "\n".join(lines)

    def _build_memory_context(self) -> str:
        if self.facts.memory != "project":
            return ""
        memory_mgr = getattr(self.deps, "memory_mgr", None)
        if memory_mgr is None:
            return ""
        return memory_mgr.build_context()

    def _build_session_context(self) -> list[ExternalContextItem]:
        session = getattr(self.deps, "session", None)
        session_context = getattr(session, "context", None)
        if not session_context:
            return []
        return [item for item in session_context if item.content]

    def _build_manager_guidance(self) -> str:
        """收集当前 agent 实际具备能力的固定 Manager 工作流。"""
        guidance: list[str] = []

        task_mgr = self.facts.task_mgr
        if task_mgr is not None:
            guidance.append(task_mgr.system_guidance())

        if not self.facts.is_subagent:
            subagent_mgr = self.facts.subagent_mgr
            if subagent_mgr is not None and subagent_mgr.describe():
                guidance.append(subagent_mgr.system_guidance())

        skill_mgr = self.facts.skill_mgr
        if skill_mgr is not None and skill_mgr.describe():
            guidance.append(skill_mgr.system_guidance())

        if self.facts.memory == "project":
            memory_mgr = getattr(self.deps, "memory_mgr", None)
            if memory_mgr is not None:
                guidance.append(memory_mgr.system_guidance())

        if not guidance:
            return ""
        return "# Manager 工作流\n" + "\n\n".join(guidance)

    def build_capability_instructions(self, mode: RunMode) -> str:
        """生成当前 agent 的框架能力边界说明。

        工具参数仍以完整 schema 为准；此处只说明当前 agent 可以执行哪些
        工具，以及 manifest、mode、feature 和主/子 agent 范围造成的限制。

        Args:
            mode: 当前运行模式（运行时权威值由调用方提供）。

        Returns:
            能力边界说明正文；无内容时返回空字符串。
        """
        tools_mgr = getattr(self.deps, "tools_mgr", None)
        declared = self.facts.tools
        features = set(self.facts.features)
        is_subagent = self.facts.is_subagent

        if tools_mgr is None:
            allowed = sorted(declared) if declared is not None else []
            blocked: list[str] = []
        else:
            names = tools_mgr.all_tool_names()
            candidates = names if declared is None else set(declared)
            allowed = []
            blocked = []
            for name in sorted(candidates):
                entry = tools_mgr.get(name)
                if entry is None:
                    # 动态 MCP 工具可能在 manifest 扫描后才注册；明确显示为
                    # 暂不可用，避免模型把声明名称误认为当前可调用 schema。
                    blocked.append(name)
                    continue
                unavailable = mode not in entry.availability.modes
                unavailable = unavailable or (
                    entry.availability.feature is not None
                    and entry.availability.feature not in features
                )
                unavailable = unavailable or (
                    is_subagent
                    and entry.availability.audience is ToolAudience.MAIN_ONLY
                )
                (blocked if unavailable else allowed).append(name)

        lines = [
            "# 当前 Agent 能力边界",
            f"当前 agent：{self.facts.agent_type}",
            f"当前模式：{mode.display_name}",
        ]
        if allowed:
            lines.append("允许执行的工具：" + ", ".join(f"`{name}`" for name in allowed))
        else:
            lines.append("允许执行的工具：无")
        if blocked:
            lines.append("已声明但当前不可执行（mode、feature、agent 范围或尚未注册）工具：" + ", ".join(
                f"`{name}`" for name in blocked
            ))
        if declared is None:
            lines.append("未声明 agent 级工具白名单；schema 中的工具均按当前授权策略处理。")
        else:
            lines.append("未列出的已注册工具不属于当前 agent 的执行白名单。")
        if features:
            lines.append("已启用 feature：" + ", ".join(f"`{name}`" for name in sorted(features)))
        return "\n".join(lines)

    def _build_static_prompt(self) -> str:
        """组装 Agent 生命周期内固定不变的 system prompt。

        只有框架固定规则和已激活的 role.md 进入 system。环境、记忆、目录和 Hook
        上下文属于外部数据，在 ``build_initial_context_messages`` 中以 user 角色提供。
        """
        sections = []

        sections.append(self._build_core())
        sections.append(self._build_execution_guidance())
        sections.append(
            "# 工具协议\n"
            "当前工具 schema 是工具名称、参数、类型、枚举和默认值的唯一权威；不要猜测未提供的工具或字段。"
            "互不依赖且不会有冲突的调用在同一轮发出，工具返回的 ID 或 session 句柄按对应后续工具的 schema 继续传递。\n"
            "失败时检查 error_code、error_details 和 recovery，修正具体原因，不原样重试、不自动改用有副作用的操作。"
            "外部程序输出和退出码由你判断，框架不推断业务成功或失败。truncated 是模型输出裁剪，UI 折叠不代表模型内容丢失。"
        )

        sections.append(render_tag_guide(PromptConsumer.WORKING_AGENT))

        web_access_mgr = getattr(self.deps, "web_access_mgr", None)
        if web_access_mgr is not None:
            sections.append(web_access_mgr.system_guidance())

        return "\n\n".join(s for s in sections if s)

    def build_initial_context_messages(self) -> list[dict]:
        """构建首次 chat 前追加的非指令上下文消息。"""
        items: list[ExternalContextItem] = []

        if not self.facts.is_subagent:
            subagent_mgr = self.facts.subagent_mgr
            if subagent_mgr is not None:
                subagents = subagent_mgr.describe()
                if subagents:
                    items.append(ExternalContextItem(
                        "subagent_catalog", "# 可用子智能体\n" + subagents,
                    ))

        skill_mgr = self.facts.skill_mgr
        if skill_mgr is not None:
            skills = skill_mgr.describe()
            if skills:
                items.append(ExternalContextItem(
                    "skill_catalog", "# 可用技能\n" + skills,
                ))

        items.extend(self._build_agents_context())

        memory_context = self._build_memory_context()
        if memory_context:
            items.append(ExternalContextItem("project_memory", memory_context))

        items.extend(self._build_session_context())
        items.append(ExternalContextItem(
            "runtime_environment", self._build_environment_context(),
        ))

        blocks = [render_external_context(item) for item in items if item.content.strip()]
        if not blocks:
            return []
        return [{"role": "user", "content": "\n\n".join(blocks)}]

    def build_manager_instructions(self) -> str:
        """构建首次真实用户请求前追加的 Manager 工作流。"""
        return self._build_manager_guidance()

    def build_mode_instructions(self, mode: RunMode) -> str:
        """构建当前模式的框架指令，供 Agent 在 chat 前追加。

        Args:
            mode: 当前运行模式（运行时权威值由调用方提供）。

        Returns:
            当前模式的框架指令正文。
        """
        sections: list[str] = []
        if mode is RunMode.PLAN:
            plan_mgr = getattr(self.deps, "plan_mgr", None)
            if plan_mgr is not None:
                sections.append(plan_mgr.instructions(self.facts.is_subagent))
        else:
            sections.append(
                "# 当前模式：执行模式\n"
                "当前直接实施用户请求；使用权限允许的工具完成修改、验证和交付。"
            )
        return "\n\n".join(section for section in sections if section)

    def build(self) -> list:
        """返回 Agent 生命周期内固定不变的 system prompt。

        Returns:
            单条固定 system 消息。
        """
        if self._system_content is None:
            self._system_content = self._build_static_prompt()
        return [{"role": "system", "content": self._system_content}]
