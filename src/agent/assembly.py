"""Agent 装配 — 把不可变 spec 装配为 agent 级部件。

装配器是 `bootstrap.create_app()` 之外唯一的 Manager 具体实现实例化点：
`Agent.__post_init__` 只消费这里产出的 `AgentAssembly`，不再自建 Manager，
也不再承担模型解析、feature 解析和工具校验。

`AgentAssembly` 跨轮次稳定：切换模型时由 `AgentAssembler.rebuild_model()`
重新装配模型相关部件并整体替换，不做就地修改，避免出现两份权威。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, TYPE_CHECKING
from uuid import UUID

from src.agent.spec import AgentSpec
from src.mgr import CompactMgr, ReminderMgr, SkillMgr, SubAgentMgr, TaskManager
from src.mgr.prompt_mgr import PromptFacts, PromptMgr

if TYPE_CHECKING:
    from src.agent.agent import AgentDeps


@dataclass(frozen=True)
class AgentAssembly:
    """一次 agent 装配的产物。

    Attributes:
        llm: 已解析的 provider 实例。
        model: 解析后的 ``供应商/模型ID`` 展示名。
        compact_mgr: 带调用方身份的上下文压缩管理器。
        prompt_mgr: 提示词组装器。
        reminder_mgr: 提醒注入管理器。
        task_mgr: 任务管理器；未启用 feature 时为 None。
        skill_mgr: 技能管理器；未启用 feature 时为 None。
        subagent_mgr: 子 agent 管理器；未启用 feature 或本实例是子 agent 时为 None。
        system_prompt: Agent 生命周期内固定的 system 消息。
    """

    llm: Any
    model: str
    compact_mgr: CompactMgr
    prompt_mgr: PromptMgr
    reminder_mgr: ReminderMgr
    task_mgr: TaskManager | None
    skill_mgr: SkillMgr | None
    subagent_mgr: SubAgentMgr | None
    system_prompt: list[dict]


class AgentAssembler:
    """按 spec 与 feature 集装配 agent 级部件与初始模型部件。

    Args:
        deps: bootstrap 组装的进程级依赖。
    """

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    def assemble(self, spec: AgentSpec, *, agent_uuid: UUID) -> AgentAssembly:
        """装配一个 agent 的全部 agent 级部件。

        Args:
            spec: 不可变的身份与配置。
            agent_uuid: 本 agent 实例 UUID，用于压缩与事件关联。

        Returns:
            已完成装配的不可变部件集合。

        Raises:
            LLMConfigurationError: 声明的工具未注册或模型不可用。
        """
        llm, model = self._resolve_llm(spec.requested_model)
        self._validate_declared_tools(spec)
        task_mgr = self._build_task_mgr(spec)
        reminder_mgr = ReminderMgr()
        if task_mgr is not None:
            reminder_mgr.register(task_mgr)
        skill_mgr = self._build_skill_mgr(spec)
        subagent_mgr = self._build_subagent_mgr(spec)
        prompt_mgr = PromptMgr(
            facts=PromptFacts(
                agent_type=spec.agent_type,
                is_subagent=spec.is_subagent,
                role_prompt=spec.role_prompt,
                tools=spec.tools,
                features=spec.features,
                memory=spec.memory,
                task_mgr=task_mgr,
                subagent_mgr=subagent_mgr,
                skill_mgr=skill_mgr,
            ),
            deps=self.deps,
            workdir=self.deps.workdir,
            global_dir=self.deps.global_dir,
        )
        return AgentAssembly(
            llm=llm,
            model=model,
            compact_mgr=self.build_compact_mgr(spec, llm, agent_uuid=agent_uuid),
            prompt_mgr=prompt_mgr,
            reminder_mgr=reminder_mgr,
            task_mgr=task_mgr,
            skill_mgr=skill_mgr,
            subagent_mgr=subagent_mgr,
            system_prompt=prompt_mgr.build(),
        )

    def rebuild_model(
        self,
        assembly: AgentAssembly,
        spec: AgentSpec,
        *,
        agent_uuid: UUID,
        model: str,
    ) -> AgentAssembly:
        """重新装配模型相关部件，保留压缩进展，返回整体替换后的装配产物。

        Args:
            assembly: 当前装配产物。
            spec: 本 agent 的不可变身份与配置。
            agent_uuid: 本 agent 实例 UUID。
            model: 新模型请求值（别名或 ``供应商/模型ID``）。

        Returns:
            仅 llm、model 与 compact_mgr 更新后的新装配产物。
        """
        llm, display_name = self._resolve_llm(model)
        compact_mgr = self.build_compact_mgr(spec, llm, agent_uuid=agent_uuid)
        compact_mgr.has_compacted = assembly.compact_mgr.has_compacted
        return replace(
            assembly, llm=llm, model=display_name, compact_mgr=compact_mgr,
        )

    def build_compact_mgr(
        self, spec: AgentSpec, llm: Any, *, agent_uuid: UUID,
    ) -> CompactMgr:
        """按当前模型上下文窗口与 ``compact`` 配置创建压缩管理器。

        Args:
            spec: 本 agent 的不可变身份与配置。
            llm: 已解析的 provider 实例。
            agent_uuid: 本 agent 实例 UUID。

        Returns:
            绑定调用方身份的 CompactMgr。
        """
        compact_cfg = self.deps.config_mgr.get_config("compact")
        context_limit = llm.context_limit
        return CompactMgr(
            llm=llm,
            caller_agent_type=spec.agent_type,
            caller_uuid=str(agent_uuid),
            auto_compact_size=int(context_limit * compact_cfg["auto_compact_rate"]),
            keep_recent_user_turns=compact_cfg.get("keep_recent_user_turns", 3),
            recent_messages_token_limit=int(
                context_limit * compact_cfg.get("keep_recent_messages_token_rate", 0.25)
            ),
            data_guard=self.deps.data_guard,
        )

    def _resolve_llm(self, model: str | None) -> tuple[Any, str]:
        """解析模型请求值并返回 provider 与其展示名。"""
        llm = self.deps.llm_mgr.get(model)
        return llm, f"{llm.provider_name}/{llm.model}"

    def _validate_declared_tools(self, spec: AgentSpec) -> None:
        """校验 manifest 声明的工具均已注册。

        ``tools_mgr`` 允许是仅提供 schema 目录的测试桩，因此按能力探测
        校验方法，而不是假设它一定实现了校验入口。
        """
        tools_mgr = getattr(self.deps, "tools_mgr", None)
        validate_tools = getattr(tools_mgr, "validate_declared_tools", None)
        if not callable(validate_tools):
            return
        try:
            validate_tools(
                spec.tools,
                source=f"Agent {spec.agent_type}",
                defer_dynamic=True,
            )
        except ValueError as exc:
            from src.llm.errors import LLMConfigurationError

            raise LLMConfigurationError(str(exc)) from exc

    def _build_task_mgr(self, spec: AgentSpec) -> TaskManager | None:
        """按 ``task`` feature 创建任务管理器。主 agent 落盘，子 agent 纯内存。"""
        if "task" not in spec.features:
            return None
        tasks_dir = None
        if not spec.is_subagent and self.deps.global_dir and self.deps.session.id:
            tasks_dir = self.deps.global_dir / "tasks" / self.deps.session.id
        on_change = self.deps.task_change_notifier if not spec.is_subagent else None
        return TaskManager(
            tasks_dir=tasks_dir,
            data_guard=self.deps.data_guard,
            on_change=on_change,
        )

    def _build_skill_mgr(self, spec: AgentSpec) -> SkillMgr | None:
        """按 ``skill`` feature 创建技能管理器。"""
        if "skill" not in spec.features:
            return None
        return SkillMgr(
            self.deps.workdir,
            global_dir=self.deps.global_dir,
            plugin_mgr=self.deps.plugin_mgr,
            role_mgr=self.deps.role_mgr,
        )

    def _build_subagent_mgr(self, spec: AgentSpec) -> SubAgentMgr | None:
        """按 ``subagent`` feature 创建子 agent 管理器。

        子 agent 不具备委派能力（``task_delegator`` 是 MAIN_ONLY），因此不
        构建该管理器，省掉每个子 agent 一次四层目录扫描与工具校验。
        """
        if spec.is_subagent or "subagent" not in spec.features:
            return None
        return SubAgentMgr(
            self.deps.workdir,
            self.deps,
            global_dir=self.deps.global_dir,
        )
