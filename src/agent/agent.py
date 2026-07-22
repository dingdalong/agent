from __future__ import annotations

import asyncio, json, logging, time, uuid
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING
from src.tools import ToolDict
from src.events.types import (
    AgentStateChanged,
    CompactDelta,
    LLMCallFailed,
    PermissionModeChanged,
    caller_identity,
)
from src.agent.states import AgentState, RunContext, RunResult, parse_command
from src.events import NoEventSubscribers, emit_telemetry_safely
from src.llm.errors import LLMCallError, LLMErrorInfo, LLMErrorKind
from src.mgr import FileMgr, TaskManager, CompactMgr, CompactResult, PromptMgr, SkillMgr, SubAgentMgr, ReminderMgr

if TYPE_CHECKING:
    from src.mgr.llm_mgr import LLMMgr
    from src.interfaces.base import UserInterface
    from src.events.bus import EventBus
    from src.mgr.tools_mgr import ToolsMgr
    from src.mgr.permission_mgr import PermissionManager, PermissionMode
    from src.mgr.config_mgr import ConfigManager
    from src.mgr.memory_mgr import MemoryMgr
    from src.mgr.hooks_mgr import HooksMgr
    from src.mgr.plan_mgr import PlanMgr
    from src.mgr.plugin_mgr import PluginMgr
    from src.mgr.session_mgr import SessionMgr
    from src.mgr.mcp_mgr import McpMgr
    from src.mgr.role_mgr import RoleMgr, AgentManifest
    from src.interfaces.turn_clock import TurnClock

logger = logging.getLogger(__name__)


_TRANSIENT_LLM_ERROR_KINDS = {
    LLMErrorKind.NETWORK,
    LLMErrorKind.TIMEOUT,
    LLMErrorKind.RATE_LIMIT,
    LLMErrorKind.SERVICE,
    LLMErrorKind.RESPONSE_PROTOCOL,
}
_REQUEST_LLM_ERROR_KINDS = {
    LLMErrorKind.BAD_REQUEST,
    LLMErrorKind.NOT_FOUND,
    LLMErrorKind.PAYLOAD_TOO_LARGE,
    LLMErrorKind.UNPROCESSABLE,
}


def _llm_failure_advice(kind: LLMErrorKind) -> str:
    """返回与 LLM 错误分类匹配的操作建议。

    Args:
        kind: 稳定 LLM 错误分类。

    Returns:
        面向用户的单句操作建议。
    """
    if kind is LLMErrorKind.CONTEXT_LIMIT:
        return "请缩小任务范围或重新开始较短的会话后重试。"
    if kind is LLMErrorKind.OUTPUT_LIMIT:
        return "请缩小输出范围后重试。"
    if kind is LLMErrorKind.CONTENT_POLICY:
        return "请调整请求内容后重试。"
    if kind is LLMErrorKind.AUTHENTICATION:
        return "请检查 API 凭据和模型配置后重试。"
    if kind in {LLMErrorKind.PERMISSION, LLMErrorKind.BILLING_QUOTA}:
        return "请检查账号权限、额度和模型配置后重试。"
    if kind in _REQUEST_LLM_ERROR_KINDS:
        return "请检查模型配置和请求内容后重试。"
    if kind in _TRANSIENT_LLM_ERROR_KINDS:
        return "请稍后重试。"
    return "请检查错误信息和模型配置后重试。"


def _format_llm_failure_text(error: LLMErrorInfo) -> str:
    """组合不重复标点或操作建议的安全失败文本。

    Args:
        error: 已安全化的结构化 LLM 错误。

    Returns:
        包含错误分类、摘要和准确操作建议的文本。
    """
    message = error.message.strip() or "LLM 调用失败"
    if message[-1] not in "。.!！?？":
        message += "。"
    advice = _llm_failure_advice(error.kind)
    advice_core = advice.rstrip("。.!！?？")
    if advice_core not in message:
        message += advice
    return f"错误：LLM 调用失败（{error.kind.value}）：{message}"


def _resolve_memory_scope(manifest_memory: str | None, is_subagent: bool) -> str | None:
    """解析 agent 的记忆范围。

    Args:
        manifest_memory: manifest 中声明的记忆范围，未声明为 None。
        is_subagent: 是否为子 agent。

    Returns:
        记忆范围字符串；manifest 显式声明则用其值，未声明时按类型取默认——
        主 agent → "project"（加载项目记忆），子 agent → None（不加载）。
    """
    if manifest_memory is not None:
        return manifest_memory
    return None if is_subagent else "project"


@dataclass
class AgentDeps:
    """外部依赖（进程级全局对象）。

    /clear 时通过 hasattr(mgr, "reload") 判断并调用，
    仅在管理器有运行时可变状态需要重置时才实现 reload() 方法。
    """

    llm_mgr: LLMMgr = None
    ui: UserInterface = None
    event_bus: EventBus = None
    tools_mgr: ToolsMgr = None
    permission_mgr: PermissionManager | None = None
    config_mgr: ConfigManager = None
    memory_mgr: MemoryMgr | None = None
    hooks_mgr: HooksMgr | None = None
    plan_mgr: PlanMgr | None = None
    plugin_mgr: PluginMgr | None = None
    session_mgr: SessionMgr | None = None
    mcp_mgr: McpMgr | None = None
    role_mgr: RoleMgr | None = None
    turn_clock: TurnClock | None = None
    permission_mode_controller: Any = None
    session_context: list[str] = field(default_factory=list)
    session_id: str = ""
    workdir: Path | None = None
    global_dir: Path | None = None

@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        uuid: 唯一类型标识。
        agent_type: agent类型
        description: 一句话描述
        permission_mode: 本 agent 的权限模式；构造时传 None 表示回退到 default_mode，
            __post_init__ 结束后恒为真正的 PermissionMode。
    """

    uuid: UUID = field(init=False)
    agent_type: str
    description: str
    deps: AgentDeps = field(repr=False)
    role_prompt: str | None = field(default=None)
    tools: set[str] | None = field(default=None)
    is_subagent: bool = field(default=False)
    memory: str | None = field(default=None)
    model: str | None = field(default=None)
    enable_thinking: bool = field(default=True)
    features: set[str] | None = field(default=None)
    _pre_plan_mode: PermissionMode | None = field(init=False, default=None)
    permission_mode: PermissionMode | None = field(default=None)
    history: list[dict] = field(init=False, default_factory=list)
    _tools_schemas: list[ToolDict] = field(init=False)
    _excluded_tools: set[str] = field(init=False, default_factory=set)
    _task_mgr: TaskManager | None = field(init=False, repr=False)
    _compact_mgr: CompactMgr = field(init=False, repr=False)
    _file_mgr: FileMgr | None = field(init=False, repr=False)
    _skill_mgr: SkillMgr | None = field(init=False, repr=False)
    _subagent_mgr: SubAgentMgr | None = field(init=False, repr=False)
    _prompt_mgr: PromptMgr = field(init=False, repr=False)
    _reminder_mgr: ReminderMgr = field(init=False, repr=False)
    _pending_input: str = field(init=False, default="")
    _handlers: dict[AgentState, Callable] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """解析依赖并初始化本 Agent 的 Manager、工具与状态 handler。

        Returns:
            None。
        """
        self.uuid = uuid.uuid4()
        # 主、子 agent 均以单实例 UUID 关联生命周期、usage 与转录事件，
        # 供 AgentViewStore 汇聚成一致快照。
        # 未显式指定权限模式时回退到 permission_mgr.default_mode（缺失则全局 DEFAULT_MODE）
        if self.permission_mode is None:
            from src.mgr.permission_mgr import DEFAULT_MODE
            pm = self.deps.permission_mgr
            self.permission_mode = pm.default_mode if pm is not None else DEFAULT_MODE
        self.llm = self.deps.llm_mgr.get(self.model)
        # 解析本 agent 启用的 feature 集，据此过滤工具、按需创建各可插拔 Manager
        from src.mgr import resolve_features
        self.features = resolve_features(self.features)
        self._excluded_tools = self.deps.tools_mgr.excluded_tool_names(self.features)
        self.refresh_tools_schemas()
        compact_cfg = self.deps.config_mgr.get_config("compact")
        context_limit = self.llm.context_limit
        self._compact_mgr = CompactMgr(
            llm=self.llm,
            workdir=self.deps.workdir,
            caller_agent_type=self.agent_type,
            caller_uuid=str(self.uuid),
            auto_compact_size=int(context_limit * compact_cfg["auto_compact_rate"]),
            keep_recent_user_turns=compact_cfg.get("keep_recent_user_turns", 3),
            recent_messages_token_limit=int(context_limit * compact_cfg.get("keep_recent_messages_token_rate", 0.25)),
        )
        workdir = self.deps.workdir
        # 可插拔 Manager：仅启用对应 feature 时创建，否则为 None（其工具已从 schema 排除）
        self._file_mgr = FileMgr(workdir, self.deps) if "file" in self.features else None
        self._skill_mgr = (
            SkillMgr(workdir, global_dir=self.deps.global_dir, plugin_mgr=self.deps.plugin_mgr, role_mgr=self.deps.role_mgr)
            if "skill" in self.features else None
        )
        self._subagent_mgr = (
            SubAgentMgr(workdir, self.deps, global_dir=self.deps.global_dir)
            if "subagent" in self.features else None
        )
        self._prompt_mgr = PromptMgr(agent=self, model=self.llm.model, workdir=workdir, global_dir=self.deps.global_dir, role_prompt=self.role_prompt)
        # 主 agent：持久化到磁盘；子 agent：纯内存模式，独立实例。
        if "task" in self.features:
            tasks_dir = None
            if not self.is_subagent and self.deps.global_dir and self.deps.session_id:
                tasks_dir = self.deps.global_dir / "tasks" / self.deps.session_id
            self._task_mgr = TaskManager(tasks_dir=tasks_dir)
        else:
            self._task_mgr = None
        self._reminder_mgr = ReminderMgr()
        if self._task_mgr is not None:
            self._reminder_mgr.register(self._task_mgr)
        self._handlers = {
            AgentState.REQUEST_INPUT:    self._on_request_input,
            AgentState.CHECK_COMPACT:    self._on_check_compact,
            AgentState.COMPACT:          self._on_compact,
            AgentState.LLM_CALL:         self._on_llm_call,
            AgentState.PROCESS_RESPONSE: self._on_process_response,
            AgentState.LENGTH_RETRY:     self._on_length_retry,
            AgentState.PAUSE_TURN:       self._on_pause_turn,
            AgentState.EXECUTE_TOOLS:    self._on_execute_tools,
            AgentState.CHECK_STOP:       self._on_check_stop,
            AgentState.POST_ROUND:       self._on_post_round,
            AgentState.SUMMARIZE_EXIT:   self._on_summarize_exit,
            AgentState.CONTEXT_OVERFLOW: self._on_context_overflow,
            AgentState.LLM_FAILURE:      self._on_llm_failure,
        }

    @classmethod
    def from_manifest(
        cls,
        manifest: AgentManifest | None,
        deps: AgentDeps,
        *,
        is_subagent: bool = False,
        **overrides: Any,
    ) -> Agent:
        """从 AgentManifest 创建 Agent 实例。

        将 manifest 的每个字段映射到 Agent 构造参数。
        manifest 为 None 时创建最小 Agent（回退用途）。
        **overrides 允许调用方覆盖任意字段。

        Args:
            manifest: 解析后的 manifest，或 None。
            deps: 外部依赖。
            is_subagent: 是否为子 agent。
            **overrides: 需覆盖的 Agent 字段。

        Returns:
            Agent 实例。
        """
        if manifest is None:
            return cls(
                agent_type="agent",
                description="",
                deps=deps,
                role_prompt=None,
                is_subagent=is_subagent,
                memory=_resolve_memory_scope(None, is_subagent),
            )
        kwargs: dict[str, Any] = dict(
            agent_type=manifest.agent_type,
            description=manifest.description,
            deps=deps,
            role_prompt=manifest.prompt,
            tools=manifest.tools,
            is_subagent=is_subagent,
            memory=_resolve_memory_scope(manifest.memory, is_subagent),
            model=manifest.model,
            permission_mode=manifest.permission_mode,
            enable_thinking=(
                manifest.enable_thinking
                if manifest.enable_thinking is not None
                else True
            ),
            features=manifest.features,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def refresh_tools_schemas(self) -> None:
        """刷新工具 schema 列表（减去被禁用 feature 的工具，再按当前权限模式过滤可见性）。"""
        names = self.tools if self.tools is not None else self.deps.tools_mgr.all_tool_names()
        names = names - self._excluded_tools
        self._tools_schemas = self.deps.tools_mgr.get_schemas(
            names,
            permission_mgr=self.deps.permission_mgr,
            mode=self.permission_mode,
        )

    def set_permission_mode(self, mode) -> bool:
        """切换本 agent 的权限模式，处理计划模式的特殊进入/退出逻辑。

        统一入口：/mode 命令、Shift+Tab 轮转、prompt_selection 菜单均通过此方法切换。
        仅主 agent 走此路径；子 agent 模式构造后固定不变。

        Args:
            mode: 目标权限模式（PermissionMode 实例）。

        Returns:
            模式是否发生了变化。
        """
        from src.mgr.permission_mgr import PLAN_MODE
        plan_mgr = self.deps.plan_mgr

        if mode is PLAN_MODE and plan_mgr is not None:
            return plan_mgr.enter_mode(self, self._reminder_mgr)

        if self.permission_mode is PLAN_MODE and plan_mgr is not None:
            plan_mgr.exit_mode(self, self._reminder_mgr)

        if mode is self.permission_mode:
            return False

        self.permission_mode = mode
        return True

    async def run(self, input: str | None = None) -> RunResult:
        """运行 agent 对话。

        交互模式（input=None）下内部循环多轮对话，仅在 exit 或 /clear 时返回；
        子智能体模式（input 不为 None）下执行单轮后立即返回。

        Args:
            input: 用户输入文本。为 None 时从 REQUEST_INPUT 开始并循环；
                   不为 None 时从 CHECK_COMPACT 开始执行单轮（子智能体路径）。

        Returns:
            RunResult，包含最终文本、斜杠命令、退出请求和终态 LLM 错误。
        """
        if input is not None:
            ctx = RunContext(
                messages=self.history,
                turn_start_messages=list(self.history),
                round_start_idx=len(self.history),
            )
            turn_instr = self._reminder_mgr.build_turn_start_instructions(self.permission_mode)
            if turn_instr:
                input = f"{turn_instr}\n\n{input}"
            self.history.append({"role": "user", "content": input})
            ctx.user_input = input
            result = await self._run_single_turn(ctx, AgentState.CHECK_COMPACT)
            if not ctx.has_tool_calls:
                self.llm.clear_reasoning_content(self.history[ctx.round_start_idx:])
            return result

        while True:
            ctx = RunContext(messages=self.history, round_start_idx=len(self.history))
            result = await self._run_single_turn(ctx, AgentState.REQUEST_INPUT)
            # 每轮结束后持久化会话历史和元数据
            self._persist_session(ctx.user_input)
            if result.exit_requested or result.command is not None:
                return result
            if not ctx.has_tool_calls:
                self.llm.clear_reasoning_content(self.history[ctx.round_start_idx:])

    async def _run_single_turn(self, ctx: RunContext, start_state: AgentState) -> RunResult:
        """运行状态机从 start_state 到 DONE 的单轮执行。

        Args:
            ctx: 当前运行上下文。
            start_state: 起始状态。

        Returns:
            RunResult，包含本轮结果和安全结构化的终态 LLM 错误。
        """
        state = start_state
        try:
            while state != AgentState.DONE:
                prev = state
                try:
                    state = await self._handlers[state](ctx)
                except LLMCallError as exc:
                    self._rollback_response_recovery(ctx)
                    ctx.llm_error = exc.info
                    state = (
                        AgentState.CONTEXT_OVERFLOW
                        if exc.info.kind is LLMErrorKind.CONTEXT_LIMIT
                        else AgentState.LLM_FAILURE
                    )
                await self._emit_state_changed(prev, state)
            return RunResult(
                final_text=ctx.final_text,
                command=ctx.command,
                exit_requested=ctx.exit_requested,
                user_input=ctx.user_input,
                llm_error=ctx.llm_error,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._rollback_response_recovery(ctx)
            if ctx.turn_start_messages is not None:
                self.history[:] = ctx.turn_start_messages
            else:
                del self.history[ctx.round_start_idx:]
            self._pending_input = ctx.user_input
            raise

    @staticmethod
    def _rollback_response_recovery(ctx: RunContext) -> None:
        """移除当前响应恢复链写入的临时消息并清空恢复状态。

        Args:
            ctx: 当前运行上下文；存在恢复 checkpoint 时原地回滚消息并清空恢复状态。

        Returns:
            None。
        """
        if ctx.response_recovery_start_idx is not None:
            del ctx.messages[ctx.response_recovery_start_idx:]
        ctx.response_recovery_start_idx = None
        ctx.response_recovery_response_count = 0
        ctx.pause_turn_message_idx = None
        ctx.pause_turn_continuations = 0

    async def _fail_response_recovery(
        self,
        ctx: RunContext,
        *,
        message: str,
        partial: bool,
        tool_fragment_state: str,
        original_exception_type: str,
    ) -> AgentState:
        """回滚响应恢复链并发出一次不可重试的输出上限事件。

        Args:
            ctx: 当前运行上下文。
            message: 面向用户的安全错误摘要。
            partial: 当前响应是否包含任何残片。
            tool_fragment_state: 当前工具调用残片状态。
            original_exception_type: 结构化错误中的内部类型名。

        Returns:
            LLM_FAILURE。
        """
        attempts = ctx.response_recovery_response_count
        self._rollback_response_recovery(ctx)
        ctx.llm_error = LLMErrorInfo(
            kind=LLMErrorKind.OUTPUT_LIMIT,
            message=message,
            retryable=False,
            original_exception_type=original_exception_type,
        )
        event_bus = getattr(getattr(self, "deps", None), "event_bus", None)
        if event_bus is not None:
            caller_agent_type, caller_uuid = caller_identity(self)
            await emit_telemetry_safely(event_bus, LLMCallFailed(
                timestamp=time.time(),
                source=self.agent_type,
                error_kind=ctx.llm_error.kind.value,
                safe_message=ctx.llm_error.message,
                attempts=attempts,
                partial=partial,
                tool_fragment_state=tool_fragment_state,
                status_code=ctx.llm_error.status_code,
                provider_code=ctx.llm_error.provider_code,
                request_id=ctx.llm_error.request_id,
                diagnostic_id=f"llm_{uuid.uuid4().hex[:12]}",
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            ))
        return AgentState.LLM_FAILURE

    # ---- state handlers ----

    async def _on_request_input(self, ctx: RunContext) -> AgentState:
        """REQUEST_INPUT: 收集用户输入、解析命令、执行 UserPromptSubmit hook。

        Args:
            ctx: 当前运行上下文。

        Returns:
            下一个状态：DONE（退出/命令）、REQUEST_INPUT（hook 阻断）或 CHECK_COMPACT。
        """
        try:
            user_input = await self.deps.event_bus.request_input(
                "\n\n你: ",
                default=self._pending_input,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
            ctx.exit_requested = True
            current_task = asyncio.current_task()
            if current_task is not None:
                while current_task.cancelling():
                    current_task.uncancel()
            return AgentState.DONE

        self._pending_input = ""
        ctx.user_input = user_input

        if user_input.strip().lower() in ("exit", "quit"):
            ctx.exit_requested = True
            return AgentState.DONE

        cmd = parse_command(user_input)
        if cmd is not None:
            cmd_name, cmd_args = cmd
            if cmd_name == "plan":
                await self._handle_plan_command()
                return AgentState.REQUEST_INPUT
            if cmd_name == "mode":
                await self._handle_mode_command()
                return AgentState.REQUEST_INPUT
            if cmd_name == "clear":
                ctx.command = cmd
                return AgentState.DONE
            if cmd_name == "agents":
                # 子 agent 视图归 app 层 AgentViewStore 持有，上抛 app 层渲染摘要
                ctx.command = cmd
                return AgentState.DONE
            if cmd_name == "resume":
                await self._handle_resume_command(cmd_args)
                return AgentState.REQUEST_INPUT
            await self.deps.event_bus.request_output(f"未知命令: /{cmd_name}\n")
            return AgentState.REQUEST_INPUT

        if self.deps.hooks_mgr is not None:
            hook_result = await self.deps.hooks_mgr.run_event(
                "UserPromptSubmit",
                user_input,
                {"prompt": user_input},
                session_id=self.deps.session_id,
                agent_id=str(self.uuid),
                agent_type=self.agent_type,
            )
            if hook_result.blocked:
                reason = hook_result.block_reason or "UserPromptSubmit hook blocked"
                await self.deps.event_bus.request_output(f"{reason}\n", markdown=True)
                return AgentState.REQUEST_INPUT
            if hook_result.additional_context:
                user_input = user_input + "\n\n" + "\n\n".join(
                    str(item) for item in hook_result.additional_context
                )

        turn_instr = self._reminder_mgr.build_turn_start_instructions(self.permission_mode)
        if turn_instr:
            user_input = f"{turn_instr}\n\n{user_input}"

        ctx.turn_start_messages = list(self.history)
        ctx.round_start_idx = len(self.history)
        self.history.append({"role": "user", "content": user_input})

        return AgentState.CHECK_COMPACT

    async def _handle_plan_command(self) -> None:
        """处理 /plan 命令：进入计划模式。"""
        from src.mgr.permission_mgr import PLAN_MODE
        if self.deps.permission_mgr is None:
            return
        if not self.set_permission_mode(PLAN_MODE):
            await self.deps.event_bus.request_output("已在计划模式中。\n")
            return
        self.refresh_tools_schemas()
        await self.deps.event_bus.emit(PermissionModeChanged(timestamp=time.time(), source=self.agent_type))
        await self.deps.event_bus.request_output("已进入计划模式。\n")

    async def _handle_resume_command(self, cmd_args: list[str]) -> None:
        """处理 /resume 命令：无参时弹出会话选择菜单，再委托 SessionMgr 解析会话并应用状态变更。

        Args:
            cmd_args: 命令参数列表，可为空（弹出选择菜单）、序号或 session_id。
        """
        session_mgr = self.deps.session_mgr
        if session_mgr is None:
            await self.deps.event_bus.request_output("会话管理器未初始化。\n")
            return

        # 无参：弹出方向键选择菜单让用户挑选历史会话，选中后转为以 session_id 解析
        if not cmd_args:
            sessions = session_mgr.list_resumable(self.deps.session_id)
            if not sessions:
                await self.deps.event_bus.request_output("没有可恢复的历史会话。\n")
                return
            options: list[tuple[str, str]] = []
            for s in sessions:
                updated = s.get("updated_at", "?")[:19].replace("T", " ")
                label = f"[{updated}] {s.get('topic') or s.get('workdir', '')}"
                options.append((s["session_id"], label))
            try:
                picked = await self.deps.event_bus.request_choice("\n最近的历史会话", options, 0)
            except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                return
            if not picked:  # Esc 取消，静默
                return
            cmd_args = [picked]

        from src.mgr.session_mgr import ResumeResult
        result = session_mgr.resolve_resume(
            cmd_args,
            current_session_id=self.deps.session_id,
            current_workdir=str(self.deps.workdir) if self.deps.workdir else "",
        )

        # 解析失败或列出会话：直接输出文本
        if isinstance(result, str):
            await self.deps.event_bus.request_output(result)
            return

        # 替换对话历史和 session_id
        self.history.clear()
        self.history.extend(result.messages)
        self.deps.session_id = result.session_id

        # 重建 TaskManager 指向恢复会话的 tasks 目录（仅在启用 task feature 时）
        if self.deps.global_dir and "task" in self.features:
            tasks_dir = self.deps.global_dir / "tasks" / result.session_id
            from src.mgr import TaskManager
            self._task_mgr = TaskManager(tasks_dir=tasks_dir)
            self._reminder_mgr = ReminderMgr()
            self._reminder_mgr.register(self._task_mgr)

        # 恢复权限模式
        mode_info = await self._restore_permission_mode(result.metadata)

        # 构建并输出恢复摘要
        topic = result.metadata.get("topic", "")
        msg_count = len(result.messages)
        task_info = ""
        if self._task_mgr is not None and self._task_mgr.has_open_items():
            task_list = self._task_mgr.list_tasks()
            open_count = sum(1 for t in task_list["tasks"] if t["status"] != "completed")
            task_info = f"，{open_count} 个未完成任务"

        await self.deps.event_bus.request_output(
            f"已恢复会话 {result.session_id[:8]}...（{msg_count} 条消息{task_info}{mode_info}）\n"
        )

        # 注入 session_context 让 LLM 知道上下文来自恢复
        self.deps.session_context.append(
            f"当前会话已从历史会话恢复（session {result.session_id[:8]}...）。"
            f"会话主题: \"{topic}\"。"
            "请基于恢复的上下文继续对话。"
        )
        self._prompt_mgr.invalidate_cache()

    async def _restore_permission_mode(self, metadata: dict) -> str:
        """从会话元数据恢复权限模式。

        处理 plan 模式的特殊恢复（先设 pre_plan_mode，再 enter_mode），
        恢复后刷新工具 schema 和 UI 状态。

        Args:
            metadata: 目标会话的元数据字典。

        Returns:
            权限模式描述文本（如 "，权限模式: plan"），无变更时返回空串。
        """
        saved_mode_value = metadata.get("permission_mode", "")
        if not saved_mode_value or self.deps.permission_mgr is None:
            return ""

        from src.mgr.permission_mgr import PLAN_MODE, parse_permission_mode, DEFAULT_MODE

        if saved_mode_value == PLAN_MODE.value:
            pre_plan_value = metadata.get("pre_plan_mode", "")
            pre_plan = parse_permission_mode(pre_plan_value) if pre_plan_value else None
            self.permission_mode = pre_plan or DEFAULT_MODE
            plan_mgr = self.deps.plan_mgr
            if plan_mgr is not None:
                plan_mgr.enter_mode(self, self._reminder_mgr)
            mode_info = "，权限模式: plan"
        else:
            mode = parse_permission_mode(saved_mode_value)
            if mode is not None:
                self.set_permission_mode(mode)
                mode_info = f"，权限模式: {mode.value}"
            else:
                return ""

        self.refresh_tools_schemas()
        await self.deps.event_bus.emit(PermissionModeChanged(timestamp=time.time(), source=self.agent_type))

        return mode_info

    async def _handle_mode_command(self) -> None:
        """处理 /mode 命令：委托给 PermissionModeController。"""
        controller = self.deps.permission_mode_controller
        if controller is not None:
            await controller.prompt_selection()

    async def _on_check_compact(self, ctx: RunContext) -> AgentState:
        """估算完整输入并推进自动 compact 状态。

        Args:
            ctx: 当前运行上下文，保存自动 compact 的进展信号。

        Returns:
            下一状态：继续调用 LLM、执行 compact，或进入退出总结。
        """
        ctx.prompt = self._prompt_mgr.build()
        if self._compact_mgr.auto_compact_size <= 0:
            ctx.compact_streak = 0
            ctx.auto_compact_before_tokens = None
            ctx.auto_compact_summarized_message_count = None
            ctx.auto_compact_has_summary = None
            return AgentState.LLM_CALL

        estimated_tokens = await asyncio.to_thread(
            self.llm.estimate_tokens,
            ctx.messages,
            ctx.prompt,
            self._tools_schemas,
        )
        needs_compact = self._compact_mgr.is_need_compact(
            ctx.messages,
            ctx.prompt,
            self._tools_schemas,
            estimated_tokens=estimated_tokens,
        )

        summarized_count = ctx.auto_compact_summarized_message_count
        before_tokens = ctx.auto_compact_before_tokens
        if summarized_count is not None and before_tokens is not None:
            failure_reason = ""
            if summarized_count == 0:
                failure_reason = "summarized_message_count=0"
            elif not ctx.auto_compact_has_summary:
                failure_reason = "summary_empty"
            elif estimated_tokens >= before_tokens:
                failure_reason = "token_count_not_decreased"

            if failure_reason:
                ctx.auto_compact_before_tokens = None
                ctx.auto_compact_summarized_message_count = None
                ctx.auto_compact_has_summary = None
                logger.warning(
                    "agent=%s compact 无有效进展: before_tokens=%d "
                    "after_tokens=%d summarized_message_count=%d reason=%s",
                    self.agent_type,
                    before_tokens,
                    estimated_tokens,
                    summarized_count,
                    failure_reason,
                )
                return AgentState.SUMMARIZE_EXIT

            ctx.auto_compact_summarized_message_count = None
            ctx.auto_compact_has_summary = None
            if not needs_compact:
                ctx.compact_streak = 0
                ctx.auto_compact_before_tokens = None
                return AgentState.LLM_CALL

            ctx.compact_streak += 1
            if ctx.compact_streak >= ctx.max_compact_streak:
                ctx.auto_compact_before_tokens = None
                logger.warning(
                    "agent=%s 连续 %d 次有效 compact 后仍需压缩: "
                    "before_tokens=%d after_tokens=%d",
                    self.agent_type,
                    ctx.max_compact_streak,
                    before_tokens,
                    estimated_tokens,
                )
                return AgentState.SUMMARIZE_EXIT

            ctx.auto_compact_before_tokens = estimated_tokens
            return AgentState.COMPACT

        if not needs_compact:
            ctx.compact_streak = 0
            ctx.auto_compact_before_tokens = None
            ctx.auto_compact_summarized_message_count = None
            ctx.auto_compact_has_summary = None
            return AgentState.LLM_CALL

        ctx.auto_compact_before_tokens = estimated_tokens
        return AgentState.COMPACT

    async def _on_compact(self, ctx: RunContext) -> AgentState:
        """执行一次自动 compact 并保存其进展信号。

        Args:
            ctx: 当前运行上下文，其消息会替换为 compact 结果。

        Returns:
            CHECK_COMPACT，以便立即重新估算 compact 后的完整输入。
        """
        caller_agent_type, caller_uuid = caller_identity(self)
        await self.deps.event_bus.emit(CompactDelta(
            timestamp=time.time(),
            source=self.agent_type,
            content="auto compact",
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))
        result = await self._compact_mgr.compact_history(ctx.messages)
        ctx.messages[:] = result.messages
        ctx.auto_compact_summarized_message_count = result.summarized_message_count
        ctx.auto_compact_has_summary = bool(result.summary.strip())
        if result.transcript_path:
            await self.deps.event_bus.request_output(f"[transcript saved: {result.transcript_path}]\n")
        return AgentState.CHECK_COMPACT

    async def _on_llm_call(self, ctx: RunContext) -> AgentState:
        """调用 LLM 并保存响应。

        Args:
            ctx: 当前运行上下文。

        Returns:
            PROCESS_RESPONSE。

        Raises:
            LLMCallError: 调用不可继续时交由单轮状态机边界收口。
        """
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        ctx.response = await self.llm.chat(
            prompt=ctx.prompt,
            messages=ctx.messages,
            tools=self._tools_schemas,
            caller_agent_type=self.agent_type,
            caller_uuid=str(self.uuid),
            enable_thinking=self.enable_thinking,
        )
        return AgentState.PROCESS_RESPONSE

    async def _on_process_response(self, ctx: RunContext) -> AgentState:
        """处理已校验响应并选择协议恢复、工具执行或停止检查。

        Args:
            ctx: 当前运行上下文；读取最近响应并更新正文、历史与恢复段状态。

        Returns:
            与响应终态对应的下一个 Agent 状态。
        """
        response = ctx.response
        if ctx.response_recovery_start_idx is None:
            ctx.final_text = response.content
        elif response.content:
            ctx.final_text += response.content

        if response.finish_reason == "length":
            return AgentState.LENGTH_RETRY
        if response.finish_reason == "pause_turn":
            return AgentState.PAUSE_TURN

        ctx.response_recovery_start_idx = None
        ctx.response_recovery_response_count = 0
        ctx.pause_turn_message_idx = None
        ctx.pause_turn_continuations = 0
        ctx.messages.append(response.assistant_message)

        if response.tool_calls:
            return AgentState.EXECUTE_TOOLS
        return AgentState.CHECK_STOP

    async def _on_length_retry(self, ctx: RunContext) -> AgentState:
        """处理因长度上限截断的响应并决定是否继续恢复。

        Args:
            ctx: 当前运行上下文；截断响应取自 ctx.response，恢复消息写入
                ctx.messages。

        Returns:
            未达到恢复上限时返回 LLM_CALL，达到上限时返回 LLM_FAILURE。
        """
        response = ctx.response
        ctx.response_recovery_response_count += 1
        assistant_message = response.assistant_message or {}
        has_truncated_tool_call = bool(
            response.tool_calls or assistant_message.get("tool_calls")
        )
        ctx.pause_turn_message_idx = None
        if ctx.response_recovery_start_idx is None:
            ctx.response_recovery_start_idx = len(ctx.messages)

        if has_truncated_tool_call:
            if response.content:
                ctx.messages.append({"role": "assistant", "content": response.content})
        else:
            ctx.messages.append(
                response.assistant_message
                or {"role": "assistant", "content": response.content or None}
            )

        if ctx.length_recoveries >= ctx.max_length_recoveries:
            if has_truncated_tool_call:
                message = (
                    "模型工具调用连续被截断，未执行不完整调用，"
                    "已达到自动恢复上限。请缩小参数范围后重试。"
                )
            else:
                message = "模型输出连续被截断，已达到自动续写恢复上限。请缩小输出范围后重试。"
            return await self._fail_response_recovery(
                ctx,
                message=message,
                partial=bool(
                    response.has_partial_data
                    or response.content
                    or has_truncated_tool_call
                    or assistant_message.get("reasoning_content")
                ),
                tool_fragment_state=(
                    "partial" if has_truncated_tool_call else "none"
                ),
                original_exception_type="OutputLengthLimit",
            )

        ctx.length_recoveries += 1
        if has_truncated_tool_call:
            retry_instruction = (
                "上一次工具调用因输出长度限制而不完整，已丢弃且未执行。"
                "请重新生成完整、有效的工具调用；若参数较长，请拆分为多个较小调用。"
                "写入大文件时请使用现有的分块能力。"
            )
        else:
            retry_instruction = "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。"
        ctx.messages.append({"role": "user", "content": retry_instruction})
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        return AgentState.LLM_CALL

    async def _on_pause_turn(self, ctx: RunContext) -> AgentState:
        """保存 pause_turn 原始载体并发起同参数协议续接。

        Args:
            ctx: 当前运行上下文；最近 pause_turn 响应取自 response。

        Returns:
            未达到协议上限时为 LLM_CALL，达到上限时为 LLM_FAILURE。
        """
        response = ctx.response
        ctx.response_recovery_response_count += 1
        if ctx.response_recovery_start_idx is None:
            ctx.response_recovery_start_idx = len(ctx.messages)

        limit = self.llm.protocol_continuation_limit("pause_turn")
        if ctx.pause_turn_continuations >= limit:
            assistant_message = response.assistant_message or {}
            return await self._fail_response_recovery(
                ctx,
                message=(
                    "模型 pause_turn 协议续接连续未完成，已达到自动恢复上限。"
                    "请缩小任务范围后重试。"
                ),
                partial=bool(
                    response.has_partial_data
                    or response.content
                    or assistant_message.get("reasoning_content")
                    or assistant_message.get("_anthropic_content")
                ),
                tool_fragment_state="none",
                original_exception_type="PauseTurnContinuationLimit",
            )

        carrier = (
            response.assistant_message
            or {"role": "assistant", "content": response.content or None}
        )
        pause_message_idx = ctx.pause_turn_message_idx
        if (
            pause_message_idx is None
            or pause_message_idx < 0
            or pause_message_idx >= len(ctx.messages)
        ):
            ctx.messages.append(carrier)
        else:
            ctx.messages[pause_message_idx] = carrier
        ctx.pause_turn_continuations += 1
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        if ctx.messages and ctx.messages[-1].get("role") == "assistant":
            ctx.pause_turn_message_idx = len(ctx.messages) - 1
        else:
            ctx.pause_turn_message_idx = None
        return AgentState.LLM_CALL

    async def _on_execute_tools(self, ctx: RunContext) -> AgentState:
        """并行执行当前轮次的所有工具调用。

        同一轮 LLM 回复中的多个工具调用通过 asyncio.gather 并发执行，
        结果按原始顺序追加到 ctx.messages。
        """
        ctx.has_tool_calls = True
        ctx.manual_compact = False
        ctx.compact_focus = None

        tool_calls = list(ctx.response.tool_calls.values())

        async def _run_one(tc: dict) -> tuple[str, str, str | None]:
            """执行单个工具调用。

            Args:
                tc: 包含 id、name、arguments 的工具调用字典。

            Returns:
                (tool_call_id, result_text, tool_name)；
                未知工具时 tool_name 为 None。
            """
            tool_name = tc["name"]
            tool_call_id = tc["id"]

            if tool_name in self._excluded_tools:
                return tool_call_id, f"错误：工具 '{tool_name}' 在当前角色下不可用", None

            if self.tools is not None and tool_name not in self.tools:
                return tool_call_id, f"错误：未知工具 '{tool_name}'", None

            try:
                args = json.loads(tc["arguments"])
                if tool_name == "compact":
                    ctx.manual_compact = True
                    ctx.compact_focus = args.get("focus")
            except json.JSONDecodeError:
                args = {}

            try:
                result_text = str(await self.deps.tools_mgr.execute(
                    tool_name, args,
                    current_tool_call_id=tool_call_id, deps=self.deps, agent=self,
                ))
            except Exception as exc:
                result_text = f"错误：工具 '{tool_name}' 执行失败: {type(exc).__name__}: {exc}"

            return tool_call_id, result_text, tool_name

        results = await asyncio.gather(*(_run_one(tc) for tc in tool_calls))

        called_tools = [name for _, _, name in results if name is not None]
        for tool_call_id, result_text, _ in results:
            ctx.messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_text,
            })

        self._reminder_mgr.notify_tool_round(called_tools)
        return AgentState.POST_ROUND

    async def _on_check_stop(self, ctx: RunContext) -> AgentState:
        if self.deps.hooks_mgr is not None and not ctx.stop_hook_used:
            stop_hook = await self.deps.hooks_mgr.run_event(
                "Stop",
                ctx.final_text,
                {"final_text": ctx.final_text},
                session_id=self.deps.session_id,
                agent_id=str(self.uuid),
                agent_type=self.agent_type,
            )
            if stop_hook.blocked:
                ctx.stop_hook_used = True
                reason = stop_hook.block_reason or "Stop hook blocked"
                ctx.messages.append({
                    "role": "user",
                    "content": f"<reminder>{reason}</reminder>",
                })
                return AgentState.CHECK_COMPACT
        return AgentState.DONE

    async def _on_post_round(self, ctx: RunContext) -> AgentState:
        for msg in self._reminder_mgr.collect_post_round_messages(self.permission_mode):
            ctx.messages.append(msg)

        if ctx.manual_compact:
            caller_agent_type, caller_uuid = caller_identity(self)
            await self.deps.event_bus.emit(CompactDelta(
                timestamp=time.time(),
                source=self.agent_type,
                content="llm manual",
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            ))
            result = await self._compact_mgr.compact_history(
                ctx.messages, focus=ctx.compact_focus,
            )
            ctx.messages[:] = result.messages
            if result.transcript_path:
                await self.deps.event_bus.request_output(f"[transcript saved: {result.transcript_path}]\n")

        return AgentState.CHECK_COMPACT

    async def _on_summarize_exit(self, ctx: RunContext) -> AgentState:
        """生成多次压缩仍无法继续时的退出总结。

        Args:
            ctx: 当前运行上下文。

        Returns:
            DONE。

        Raises:
            LLMCallError: 总结调用失败时交由单轮状态机边界收口。
        """
        summary_instruction = {
            "role": "user",
            "content": "由于对话上下文过长且多次压缩仍无法继续，请你基于当前已完成的工作做一个总结："
            "1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。",
        }
        summary_messages = self.llm.normalize_messages([
            *ctx.messages,
            summary_instruction,
        ])
        response = await self.llm.chat(
            prompt=ctx.prompt,
            messages=summary_messages,
            tools=[],
            caller_agent_type=self.agent_type,
            caller_uuid=str(self.uuid),
            enable_thinking=False,
        )
        if response.content:
            ctx.final_text = response.content
        ctx.messages[:] = summary_messages
        ctx.messages.append(response.assistant_message)
        return AgentState.DONE

    async def _on_context_overflow(self, ctx: RunContext) -> AgentState:
        """生成上下文限制错误的安全可操作文本。

        Args:
            ctx: 当前运行上下文，包含 CONTEXT_LIMIT 错误信息。

        Returns:
            DONE。
        """
        error = ctx.llm_error or LLMErrorInfo(
            kind=LLMErrorKind.CONTEXT_LIMIT,
            message="输入超过模型上下文窗口",
            retryable=False,
            original_exception_type="ContextLimit",
        )
        ctx.final_text = _format_llm_failure_text(error)
        return AgentState.DONE

    async def _on_llm_failure(self, ctx: RunContext) -> AgentState:
        """生成普通 LLM 终态错误的安全可操作文本。

        Args:
            ctx: 当前运行上下文，包含安全结构化错误信息。

        Returns:
            DONE。
        """
        error = ctx.llm_error
        if error is None:
            error = LLMErrorInfo(
                kind=LLMErrorKind.UNKNOWN,
                message="LLM 调用失败",
                retryable=False,
                original_exception_type="UnknownLLMError",
            )
        ctx.final_text = _format_llm_failure_text(error)
        return AgentState.DONE

    # ---- helpers ----

    def _persist_session(self, user_input: str = "") -> None:
        """持久化当前会话历史和元数据（仅主 agent，子 agent 跳过）。

        首次持久化时写入 is_new=True 并将用户首条消息设为 topic。
        无实际对话时（history 为空）跳过元数据写入，避免保存空会话。

        Args:
            user_input: 用户本轮原始输入，首次调用时作为会话主题。
        """
        if self.is_subagent or self.deps.session_mgr is None:
            return
        session_id = self.deps.session_id
        if not session_id:
            return
        self.deps.session_mgr.save_history(session_id, self.history)
        if user_input and self.history:
            is_new = self.deps.session_mgr.get_metadata(session_id) is None
            perm_mode = self.permission_mode.value
            pre_plan = self._pre_plan_mode.value if self._pre_plan_mode else ""
            self.deps.session_mgr.save_metadata(
                session_id,
                is_new=is_new,
                topic=user_input if is_new else "",
                permission_mode=perm_mode,
                pre_plan_mode=pre_plan,
            )

    async def _emit_state_changed(self, from_state: AgentState, to_state: AgentState) -> None:
        if self.deps.event_bus is None:
            return
        await self.deps.event_bus.emit(AgentStateChanged(
            timestamp=time.time(),
            source=self.agent_type,
            agent_id=str(self.uuid),
            agent_type=self.agent_type,
            from_state=from_state.value,
            to_state=to_state.value,
        ))
