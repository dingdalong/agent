from __future__ import annotations

import asyncio, json, logging, time, uuid
from collections import deque
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING
from src.mode import RunMode
from src.events.types import (
    AgentStateChanged,
    CompactDelta,
    LLMCallFailed,
    LLMLengthRetrying,
    UserInputAdopted,
    caller_identity,
)
from src.agent.assembly import AgentAssembly, AgentAssembler
from src.agent.spec import AgentSpec
from src.agent.states import AgentState, RunContext, RunResult, parse_command
from src.mgr.common.subagent_port import ParentAgentFacts, SubagentSpawnRequest
from src.events import NoEventSubscribers, emit_telemetry_safely
from src.llm.base import TruncationKind, classify_truncation
from src.llm.errors import LLMCallError, LLMConfigurationError, LLMErrorInfo, LLMErrorKind
from src.llm.message_view import materialize_assistant
from src.prompt_tags import (
    ExternalContextItem,
    PromptTag,
    render_external_context,
    render_prompt_tag,
    render_turn_aborted,
)

if TYPE_CHECKING:
    from src.mgr.llm_mgr import LLMMgr
    from src.interfaces.base import UserInterface
    from src.events.bus import EventBus
    from src.mgr.tools_mgr import ToolsMgr
    from src.mgr.permission_mgr import PermissionManager
    from src.mgr.config_mgr import ConfigManager
    from src.mgr.context_mgr import ContextMgr
    from src.mgr.memory_mgr import MemoryMgr
    from src.mgr.hooks_mgr import HooksMgr
    from src.mgr.plan_mgr import PlanMgr
    from src.mgr.plugin_mgr import PluginMgr
    from src.mgr.session_mgr import SessionMgr
    from src.common.session_state import SessionState
    from src.mgr.mcp_mgr import McpMgr
    from src.mgr.skill_mgr import SkillMgr
    from src.mgr.subagent_mgr import SubAgentMgr
    from src.mgr.task_mgr import TaskManager
    from src.mgr.role_mgr import RoleMgr, AgentManifest
    from src.interfaces.turn_clock import TurnClock
    from src.mgr.web_access_mgr import WebAccessMgr
    from src.commands import CommandMgr

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

# 触底/无降档阶梯时，思考截断恢复注入的框架指令。
_COMPRESS_REASONING_INSTRUCTION = (
    "上一次回复在思考阶段就耗尽了输出预算，未能给出任何正式答复。"
    "请大幅压缩思考过程：只保留必要的关键推理，直接产出面向用户的答复或工具调用，"
    "不要展开冗长的内部推理。"
)


def _tool_result_status(content: object) -> str | None:
    """读取工具消息首行元数据里的执行状态。

    工具结果由 ``ToolResult.__str__`` 序列化为「元数据 JSON + 正文」，只有元数据
    行能区分正常完成与被中止；解析失败（例如 hook 注入的纯文本）按未知状态处理。

    Args:
        content: 工具消息的 content 字段。

    Returns:
        status 字段值；缺失或无法解析时为 None。
    """
    head, _, _body = str(content).partition("\n")
    try:
        metadata = json.loads(head)
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    status = metadata.get("status")
    return status if isinstance(status, str) else None


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


@dataclass
class SessionContext:
    """会话级可变状态 — 与进程级依赖分离。

    会话边界（`AgentApp._reset_session`、`/resume`）只更新本对象，进程级
    `AgentDeps` 与其 Manager 在整个进程生命周期内保持不变。

    Attributes:
        id: 当前会话 ID；空串表示尚未开始会话。
        state: 当前会话的 `SessionState`；未初始化为 None。
        context: 会话级外部上下文条目（SessionStart hook、resume 摘要）。
        env_baseline: 静态环境基线文本，每个会话采集一次。
    """

    id: str = ""
    state: "SessionState | None" = None
    context: list[ExternalContextItem] = field(default_factory=list)
    env_baseline: str = ""
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass(frozen=True)
class AgentDeps:
    """由 bootstrap 组装、供 Agent 与显式重载流程共享的进程级依赖。

    本容器在 `create_app()` 之后不可变：会话级可变状态全部收在
    `session`（`SessionContext`）中，运行期不得就地改写 deps 字段。
    """

    llm_mgr: LLMMgr = None
    ui: UserInterface = None
    event_bus: EventBus = None
    tools_mgr: ToolsMgr = None
    process_mgr: Any = None
    permission_mgr: PermissionManager | None = None
    web_access_mgr: WebAccessMgr | None = None
    config_mgr: ConfigManager = None
    memory_mgr: MemoryMgr | None = None
    context_mgr: ContextMgr | None = None
    hooks_mgr: HooksMgr | None = None
    plan_mgr: PlanMgr | None = None
    plugin_mgr: PluginMgr | None = None
    session_mgr: SessionMgr | None = None
    mcp_mgr: McpMgr | None = None
    role_mgr: RoleMgr | None = None
    turn_clock: TurnClock | None = None
    command_mgr: CommandMgr | None = None
    data_guard: Any = None
    trust_gate: Any = None
    session: SessionContext = field(default_factory=SessionContext)
    workdir: Path | None = None
    global_dir: Path | None = None
    task_change_notifier: Callable[[list[dict]], None] | None = None

@dataclass
class Agent:
    """Agent 状态机 — 消费装配产物，只持有自身状态。

    身份与配置在冻结的 `AgentSpec`，agent 级 Manager 与初始模型部件在冻结的
    `AgentAssembly`；本类只保留逐轮变化的状态与状态机 handler，构造过程不再
    解析模型、构造 Manager 或构建提示词（见 `AgentAssembler`）。

    Attributes:
        spec: 不可变的身份与配置。
        deps: bootstrap 组装的进程级依赖。
        assembly: 装配产物。
        uuid: 唯一实例标识。
        history: 当前对话消息。
        mode: 当前运行模式（运行时权威值）。
        model: 解析后的 ``供应商/模型ID`` 展示名。
        reasoning_effort: 当前生效的推理强度覆盖；None 时跟随 provider。
    """

    spec: AgentSpec
    deps: AgentDeps = field(repr=False)
    assembly: AgentAssembly = field(repr=False)
    uuid: UUID = field(default_factory=uuid.uuid4)
    history: list[dict] = field(init=False, default_factory=list)
    mode: RunMode = field(init=False, default=RunMode.EXECUTE)
    model: str = field(init=False, default="")
    reasoning_effort: str | None = field(init=False, default=None)
    _last_injected_mode: RunMode | None = field(init=False, default=None, repr=False)
    _last_capability_mode: RunMode | None = field(init=False, default=None, repr=False)
    _initial_context_added: bool = field(init=False, default=False, repr=False)
    _initial_context_message_count: int = field(init=False, default=0, repr=False)
    _manager_guidance_injected: bool = field(init=False, default=False, repr=False)
    _queued_user_action: str = field(init=False, default="", repr=False)
    # 工作期间提交、等待在下次模型调用前并入对话的用户文本（FIFO）。
    _pending_user_inputs: deque[str] = field(init=False, default_factory=deque, repr=False)
    _handlers: dict[AgentState, Callable] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """初始化本实例的状态与状态 handler。

        装配（模型解析、feature 解析、工具校验、Manager 构造、固定 system）
        全部由 `AgentAssembler` 完成，这里只做状态初始化：模式与模型镜像、
        会话历史水合和 handler 表。

        Returns:
            None。
        """
        self.mode = self.spec.initial_mode
        self.model = self.assembly.model
        self.reasoning_effort = self.spec.initial_reasoning_effort
        if not self.spec.is_subagent and self.deps.session.state is not None:
            self.history[:] = self.deps.session.state.context_messages()
            self._initial_context_added = bool(self.history)
            self._initial_context_message_count = 1 if self.history else 0
            self._manager_guidance_injected = bool(self.history)
            self._last_capability_mode = self.mode if self.history else None
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

    # ---- 只读查询面 ----
    # 事实始终落在 spec / assembly 两处权威；外部协作者（ToolsMgr、工具实现、
    # UI 与委派路径）只通过这些只读属性读取，避免各自 getattr 猜测字段位置。

    @property
    def agent_type(self) -> str:
        """本 agent 的类型标识。"""
        return self.spec.agent_type

    @property
    def is_subagent(self) -> bool:
        """本实例是否以子 agent 身份装配。"""
        return self.spec.is_subagent

    @property
    def features(self) -> frozenset[str]:
        """本 agent 已解析启用的 feature 集。"""
        return self.spec.features

    @property
    def tools(self) -> frozenset[str] | None:
        """本 agent 声明的工具白名单；None 表示未声明。"""
        return self.spec.tools

    @property
    def enable_thinking(self) -> bool:
        """本 agent 声明是否启用模型思考。"""
        return self.spec.enable_thinking

    @property
    def llm(self) -> Any:
        """当前生效的 provider 实例。"""
        return self.assembly.llm

    @property
    def task_mgr(self) -> TaskManager | None:
        """任务管理器；未启用 task feature 时为 None。"""
        return self.assembly.task_mgr

    @property
    def skill_mgr(self) -> SkillMgr | None:
        """技能管理器；未启用 skill feature 时为 None。"""
        return self.assembly.skill_mgr

    @property
    def subagent_mgr(self) -> SubAgentMgr | None:
        """子 agent 管理器；未启用 subagent feature 或本实例是子 agent 时为 None。"""
        return self.assembly.subagent_mgr

    def switch_model(self, model: str, reasoning_effort: str) -> None:
        """原地切换模型与推理强度，保留对话历史和 Agent 身份。"""
        from src.llm.base import normalize_reasoning_effort

        effort = normalize_reasoning_effort(reasoning_effort)
        if effort is None:
            raise ValueError(f"无效推理强度: {reasoning_effort!r}")
        self.assembly = AgentAssembler(self.deps).rebuild_model(
            self.assembly, self.spec, agent_uuid=self.uuid, model=model,
        )
        self.model = self.assembly.model
        self.reasoning_effort = effort

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

        manifest 为 None 时创建最小 Agent（回退用途）；**overrides 允许
        调用方覆盖任意 spec 字段，键名与历史构造参数一致。

        Args:
            manifest: 解析后的 manifest，或 None。
            deps: 外部依赖。
            is_subagent: 是否为子 agent。
            **overrides: 需覆盖的 spec 字段。

        Returns:
            已装配完成的 Agent 实例。
        """
        return cls.from_spec(
            AgentSpec.from_manifest(manifest, is_subagent=is_subagent, **overrides),
            deps,
        )

    @classmethod
    def from_spec(
        cls,
        spec: AgentSpec,
        deps: AgentDeps,
        *,
        assembly: AgentAssembly | None = None,
        assembler: AgentAssembler | None = None,
    ) -> Agent:
        """按已解析的 spec 装配并创建 Agent 实例。

        Args:
            spec: 不可变的身份与配置。
            deps: 外部依赖。
            assembly: 已装配的部件集合；测试可注入桩件，None 时现场装配。
            assembler: 自定义装配器；None 时按 deps 新建。

        Returns:
            已装配完成的 Agent 实例。
        """
        agent_uuid = uuid.uuid4()
        if assembly is None:
            assembly = (assembler or AgentAssembler(deps)).assemble(
                spec, agent_uuid=agent_uuid,
            )
        return cls(spec=spec, deps=deps, assembly=assembly, uuid=agent_uuid)

    # ---- 子 agent 委派端口实现 ----
    # 调度（查表、任务状态、hook/事件、共享上下文记账）在 SubAgentMgr；
    # 构造与运行子 agent 属于 agent 层，因此由本类实现 SubagentExecutor。

    def delegation_facts(self) -> ParentAgentFacts:
        """导出委派路径所需的自身事实快照。

        子 agent 的思考、推理力度与 feature 继承规则消费这份快照，
        管理器因此不必回读本实例的私有字段。

        Returns:
            本次委派使用的不可变事实快照。
        """
        return ParentAgentFacts(
            uuid=str(self.uuid),
            agent_type=self.spec.agent_type,
            mode=self.mode,
            features=self.spec.features,
            enable_thinking=self.spec.enable_thinking,
            reasoning_effort=self.reasoning_effort,
            provider_reasoning_effort=getattr(
                self.assembly.llm, "reasoning_effort", None,
            ),
            task_writer=self.assembly.task_mgr,
        )

    def spawn_subagent(self, request: SubagentSpawnRequest) -> Agent:
        """按委派请求构造（不运行）子 agent。

        Args:
            request: 已解析继承值的构造请求。

        Returns:
            装配完成的子 agent 实例。

        Raises:
            LLMConfigurationError: 声明的工具未注册或模型不可用。
        """
        manifest = request.manifest
        tools_mgr = getattr(self.deps, "tools_mgr", None)
        if tools_mgr is not None:
            try:
                tools_mgr.validate_declared_tools(
                    manifest.tools,
                    source=f"子 agent 定义 {manifest.path}",
                    defer_dynamic=True,
                )
            except ValueError as exc:
                raise LLMConfigurationError(str(exc)) from exc
        return Agent.from_manifest(
            manifest,
            self.deps,
            is_subagent=True,
            tools=manifest.tools,
            model=manifest.model,
            enable_thinking=request.enable_thinking,
            reasoning_effort=request.reasoning_effort,
            features=request.features,
            mode=request.mode,
        )

    async def delegate_subagent(
        self,
        agent_type: str,
        prompt: str,
        *,
        task_id: str | None = None,
        description: str = "",
        shared_context: str = "auto",
    ) -> str:
        """把任务委派给子 agent 并返回执行结果文本。

        Args:
            agent_type: 目标子 agent 类型标识。
            prompt: 传给子 agent 的完整任务正文。
            task_id: 关联的任务 ID（可选），框架自动维护其状态。
            description: 委派摘要，进入生命周期事件与共享账本标题。
            shared_context: "auto" 注入共享账本摘要；"none" 完全隔离。

        Returns:
            子 agent 的执行结果文本，或错误信息。
        """
        subagent_mgr = self.assembly.subagent_mgr
        if subagent_mgr is None:
            return "错误: 当前 agent 未启用 subagent feature，不能委派子智能体。"
        return await subagent_mgr.delegate(
            agent_type,
            prompt,
            parent=self.delegation_facts(),
            executor=self,
            task_id=task_id,
            description=description,
            shared_context=shared_context,
        )

    def set_mode(self, mode: RunMode) -> bool:
        """切换运行模式。"""
        plan_mgr = getattr(self.deps, "plan_mgr", None)
        if mode is self.mode:
            return False
        if mode is RunMode.PLAN and plan_mgr is not None:
            return plan_mgr.enter_mode(self)
        if mode is RunMode.EXECUTE and plan_mgr is not None:
            return plan_mgr.exit_mode(self)
        self.mode = mode
        return True

    def queue_user_action(self, content: str) -> None:
        """排队一条由框架生成、在下一用户轮次处理的 action。"""
        if self._queued_user_action:
            raise RuntimeError("已有待处理的框架用户 action")
        self._queued_user_action = content

    def queue_user_input(self, text: str) -> bool:
        """排队一条用户输入：在下一次模型调用前并入当前对话。

        Args:
            text: 用户在 Agent 工作期间提交的原始文本；纯空白忽略。

        Returns:
            True 表示已受理；False 表示纯空白、没有可排队的内容。
        """
        if not text.strip():
            return False
        self._queued_inputs().append(text)
        return True

    def pending_user_inputs(self) -> list[str]:
        """按 FIFO 返回尚未并入对话的排队输入副本（供 UI 预览）。"""
        return list(self._queued_inputs())

    def take_last_pending_user_input(self) -> str | None:
        """取回最后一条排队输入（供用户改主意时编辑）；无排队时返回 None。"""
        pending = self._queued_inputs()
        if not pending:
            return None
        return pending.pop()

    def take_all_pending_user_inputs(self) -> list[str]:
        """取走全部排队输入（中断时回填输入框），不自动发送。"""
        pending = self._queued_inputs()
        taken = list(pending)
        pending.clear()
        return taken

    def _queued_inputs(self) -> deque[str]:
        """返回排队输入队列；最小化测试实例未走 ``__post_init__`` 时按需创建。"""
        pending = getattr(self, "_pending_user_inputs", None)
        if pending is None:
            pending = deque()
            self._pending_user_inputs = pending
        return pending

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
            data_guard = getattr(self.deps, "data_guard", None)
            if data_guard is not None:
                input = str(data_guard.redact(input))
            self._append_initial_context_messages()
            ctx = RunContext(messages=self.history)
            self.assembly.reminder_mgr.queue_turn_start(self.mode, self.spec.is_subagent)
            self._append_pending_framework_message(ctx)
            self._append_message(self.history, {"role": "user", "content": input})
            ctx.user_message_index = len(self.history) - 1
            ctx.user_input = input
            result = await self._run_single_turn(ctx, AgentState.CHECK_COMPACT)
            await self._cleanup_tasks_at_turn_end(ctx)
            return result

        while True:
            ctx = RunContext(messages=self.history)
            result = await self._run_single_turn(ctx, AgentState.REQUEST_INPUT)
            # 流式事件完成归并后再保存与下一次 LLM 输入一致的会话状态。
            event_bus = getattr(self.deps, "event_bus", None)
            if event_bus is not None and hasattr(event_bus, "join"):
                await event_bus.join()
            await self._cleanup_tasks_at_turn_end(ctx)
            await self._commit_session(ctx.user_input)
            if result.exit_requested or result.command is not None:
                return result

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
            self._settle_stream_views()
            return RunResult(
                final_text=ctx.final_text,
                command=ctx.command,
                exit_requested=ctx.exit_requested,
                user_input=ctx.user_input,
                llm_error=ctx.llm_error,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._finalize_interrupted_turn(ctx)
            raise

    def _finalize_interrupted_turn(self, ctx: RunContext) -> None:
        """在取消边界把本轮收束成合法的模型历史。

        顺序固定为：回滚长度恢复链的临时残片 → 按整段复核脱敏 → 收束被中断的
        LLM 尝试 → 为本轮悬空工具调用补中止结果 → 按需追加 ``<turn_aborted>``。

        被中断的 LLM 尝试按部件完整度分两类：provider 已给出结束标记的部件
        （Anthropic ``content_block_stop``、Responses ``output_item.done``）可以
        原样回放，收束成一条绑定到本轮展示记录的助手消息；没有任何完整部件时，
        本轮没有可安全回放的模型输出，只丢弃本次调用尚未产出内容的模型输出，
        用户输入保留，避免把半截思考或半截工具调用发给 provider。

        「助手消息」只是历史里的 role 外壳，模型输出在历史中只能挂在 assistant
        角色下；思考仍以推理载体原样存放（Anthropic ``_anthropic_content`` 的
        thinking 块、Responses ``_response_output`` 的 reasoning 项、其余
        ``reasoning_content``），不会并入 ``content`` 正文，展示投影也仍按
        thinking 呈现。

        收尾会改动会话状态，但不在取消处理器里写盘：调用方（``AgentApp``）在
        任务真正结束后按事务路径强制提交。方法可安全重复调用：``ctx`` 上的
        守卫保证重复进入不会新增任何消息。

        Args:
            ctx: 被取消轮次的运行上下文。
        """
        if ctx.interrupt_finalized:
            return
        ctx.interrupt_finalized = True
        self._rollback_response_recovery(ctx)
        self._settle_stream_views()
        if ctx.user_message_index is None:
            return
        self._settle_interrupted_parts(ctx)
        self._complete_interrupted_tool_results(ctx)
        if not self._turn_has_aborted_tool_call(ctx):
            return
        self._append_message(
            self.history,
            {"role": "user", "content": render_turn_aborted()},
            kind="turn_aborted",
        )

    def _settle_interrupted_parts(self, ctx: RunContext) -> None:
        """把被中断的 LLM 尝试收束成模型可见的助手消息或一次回滚。

        provider 在取消时登记本次尝试上下文；没有登记说明取消发生在 LLM 调用
        之外（工具执行、提交或等待输入），此时历史已经一致，不做任何改动。

        Args:
            ctx: 被取消轮次的运行上下文。
        """
        llm = getattr(self.assembly, "llm", None)
        take_call = getattr(llm, "take_interrupted_call", None)
        call = take_call() if callable(take_call) else None
        if call is None:
            return
        message = llm.interrupted_assistant_message(call)
        if message is None:
            self._rollback_unfinished_turn(ctx)
            return
        self._append_message(
            self.history,
            message,
            correlation_id=call.call_id or None,
            kind="assistant",
        )

    def _rollback_unfinished_turn(self, ctx: RunContext) -> None:
        """丢弃本次调用没有完整模型输出的临时上下文，保留用户输入。

        回滚点固定为本次调用开始处：本次调用注入的框架消息与未完成尝试一起丢弃，
        用户输入保留在模型历史里——被中断的是模型的回答，不是用户说过的话；多轮次
        回合里已完成轮次的助手与工具消息同样保持可见。展示投影与磁盘不受影响，
        用户仍然能看到已产出的流式残片，下一条用户消息会与保留的输入一起交给模型。

        Args:
            ctx: 被取消轮次的运行上下文。
        """
        state = self._session_state()
        del self.history[ctx.llm_call_start_idx:]
        if state is not None:
            state.truncate_context(ctx.llm_call_context_start)

    async def _cleanup_tasks_at_turn_end(self, ctx: RunContext) -> None:
        """轮末收尾：turn 正常结束且全部任务 completed 时清空任务。

        Args:
            ctx: 当前运行上下文。
        """
        if self.assembly.task_mgr is None:
            return
        if ctx.llm_error is not None or ctx.exit_requested or ctx.command is not None:
            return
        await asyncio.to_thread(self.assembly.task_mgr.cleanup_if_all_completed)

    def _settle_stream_views(self) -> None:
        """把本轮已产出的流式正文按整段复核一次脱敏。

        分块脱敏看不到跨块的敏感模式；整段复核发现改写时，状态层会以整段结果
        为准并让该记录退回整份快照，保证显示、内存与磁盘一致。

        Returns:
            None。
        """
        state = self._session_state()
        if state is None:
            return
        data_guard = getattr(self.deps, "data_guard", None)
        redact = getattr(data_guard, "redact", None)
        state.settle_view_text(redact if callable(redact) else None)

    def _turn_has_aborted_tool_call(self, ctx: RunContext) -> bool:
        """判断被中止的回合是否需要 ``<turn_aborted>`` 标记。

        标记只提醒「工具或命令可能已部分生效」：本轮存在被中止（含未执行即被
        中止）的工具调用时才追加。全部工具正常返回、或本轮没有动过工具时标记
        没有信息量，只会让上下文堆叠重复的用户消息。

        Args:
            ctx: 被中止轮次的运行上下文。

        Returns:
            本轮存在未执行完就被中止的工具调用时为 True。
        """
        start = ctx.user_message_index or 0
        for message in self.history[start:]:
            if message.get("role") != "tool":
                continue
            if _tool_result_status(message.get("content")) == "cancelled":
                return True
        return False

    def _complete_interrupted_tool_results(self, ctx: RunContext) -> None:
        """保留已完成工具轮；只为本轮悬空调用补未执行结果。

        已派发但被取消的调用由 ``_on_execute_tools`` 的收尾补中止结果，这里
        只处理还没交给工具执行器的调用（例如取消发生在助手消息提交之后、
        工具轮开始之前）。回溯范围限制在本轮用户消息之后，不改写历史轮次。

        Args:
            ctx: 被取消轮次的运行上下文。
        """
        messages = self.history
        start = ctx.user_message_index or 0
        pending = {}
        for message in messages[start:]:
            if message.get("role") == "assistant":
                view = materialize_assistant(message)
                pending = {tc["id"]: tc for tc in view.get("tool_calls", [])}
            elif message.get("role") == "tool":
                pending.pop(message.get("tool_call_id"), None)
            else:
                pending = {}
        for ident in pending:
            self._append_message(
                messages,
                {
                    "role": "tool",
                    "tool_call_id": ident,
                    "content": str(self._not_executed_tool_result()),
                },
                correlation_id=ident,
                kind="tool",
            )

    @staticmethod
    def _aborted_tool_result(started_at: float) -> "ToolResult":
        """构造已派发但被用户中止的工具结果。"""
        from src.common.tool_display import ToolResult

        elapsed = max(0.0, time.monotonic() - started_at)
        return ToolResult(
            f"已运行 {elapsed:.1f}s 后被用户中止；该工具可能已经产生部分副作用。",
            status="cancelled",
            error_code="aborted_by_user",
        )

    @staticmethod
    def _not_executed_tool_result() -> "ToolResult":
        """构造尚未派发就被用户中止的工具结果。"""
        from src.common.tool_display import ToolResult

        return ToolResult(
            "该调用未执行即被用户中止。",
            status="cancelled",
            error_code="not_executed",
        )

    def _rollback_response_recovery(self, ctx: RunContext) -> None:
        """移除当前响应恢复链写入的临时消息并清空恢复状态。

        Args:
            ctx: 当前运行上下文；存在恢复 checkpoint 时原地回滚消息并清空恢复状态。

        Returns:
            None。
        """
        if ctx.response_recovery_start_idx is not None:
            self._truncate_messages(ctx.messages, ctx.response_recovery_start_idx)
        ctx.response_recovery_start_idx = None
        ctx.response_recovery_response_count = 0
        ctx.pause_turn_message_idx = None
        ctx.pause_turn_continuations = 0
        ctx.length_effort_override = None
        ctx.pending_framework_instructions.clear()

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
                source=getattr(self, "agent_type", "agent"),
                error_kind=ctx.llm_error.kind.value,
                call_id=getattr(ctx.response, "call_id", "") if ctx.response is not None else "",
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
        queued_action = getattr(self, "_queued_user_action", "")
        adopted_from_queue = False
        pending_inputs = self._queued_inputs()
        if queued_action:
            user_input = queued_action
            self._queued_user_action = ""
        elif pending_inputs:
            # 排到本次输入窗口的文本（含命令与退出词）直接作为本轮输入，
            # 不再弹出输入窗口：用户已经表达过要说什么。
            user_input = pending_inputs.popleft()
            adopted_from_queue = True
        else:
            try:
                user_input = await self.deps.event_bus.request_input(
                    "\n\n你: ",
                )
            except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
                ctx.exit_requested = True
                current_task = asyncio.current_task()
                if current_task is not None:
                    while current_task.cancelling():
                        current_task.uncancel()
                return AgentState.DONE

        data_guard = getattr(self.deps, "data_guard", None)
        if data_guard is not None:
            user_input = str(data_guard.redact(user_input))
        ctx.user_input = user_input

        state = self._session_state()
        if state is not None and user_input:
            ctx.user_record_id = state.append_user(user_input)
        if adopted_from_queue:
            await self._emit_user_input_adopted(user_input)

        if user_input.strip().lower() in ("exit", "quit"):
            ctx.exit_requested = True
            return AgentState.DONE

        cmd = parse_command(user_input)
        if cmd is not None:
            from src.commands import CommandContext, CommandSignal
            outcome = await self.deps.command_mgr.dispatch(
                cmd[0], cmd[1],
                CommandContext(deps=self.deps, agent=self),
                run_ctx=ctx,
            )
            if outcome.signal is CommandSignal.DEFER_TO_APP:
                return AgentState.DONE
            return AgentState.REQUEST_INPUT

        if self.deps.hooks_mgr is not None:
            hook_result = await self.deps.hooks_mgr.run_event(
                "UserPromptSubmit",
                user_input,
                {"prompt": user_input},
                session_id=self.deps.session.id,
                agent_id=str(self.uuid),
                agent_type=self.spec.agent_type,
            )
            if hook_result.blocked:
                reason = hook_result.block_reason or "UserPromptSubmit hook blocked"
                await self.deps.event_bus.request_output(f"{reason}\n", markdown=True)
                return AgentState.REQUEST_INPUT
            if hook_result.additional_context:
                external = "\n\n".join(str(item) for item in hook_result.additional_context)
                user_input = (
                    f"{user_input}\n\n"
                    + render_external_context(ExternalContextItem(
                        "UserPromptSubmit hook", external,
                    ))
                )

        if data_guard is not None:
            user_input = str(data_guard.redact(user_input))
        self._append_initial_context_messages()
        self.assembly.reminder_mgr.queue_turn_start(self.mode, self.spec.is_subagent)
        self._append_pending_framework_message(ctx)
        self._append_message(
            self.history,
            {"role": "user", "content": user_input},
            record_id=ctx.user_record_id,
            kind="user",
        )
        ctx.user_message_index = len(self.history) - 1
        await self._commit_session(user_input)

        return AgentState.CHECK_COMPACT

    def get_input_history(self) -> list[str]:
        """返回当前会话输入栏历史副本（时间正序）。

        子 agent 没有会话状态，返回空列表。
        """
        state = self._session_state()
        if state is not None:
            return state.input_history()
        return []

    async def _on_check_compact(self, ctx: RunContext) -> AgentState:
        """估算完整输入并推进 PreTurn/MidTurn 自动 compact 状态。

        Args:
            ctx: 当前运行上下文，保存自动 compact 的进展信号。

        Returns:
            下一状态：继续调用 LLM、执行 compact，或进入退出总结。
        """
        ctx.prompt = self.assembly.system_prompt
        if ctx.prompt is None:
            ctx.prompt = self.assembly.prompt_mgr.build()
        if self.assembly.compact_mgr.auto_compact_size <= 0:
            ctx.compact_streak = 0
            ctx.auto_compact_before_tokens = None
            ctx.auto_compact_summarized_message_count = None
            ctx.auto_compact_has_summary = None
            return AgentState.LLM_CALL

        estimated_tokens = await asyncio.to_thread(
            self.assembly.llm.estimate_tokens,
            ctx.messages,
            ctx.prompt,
            self.deps.tools_mgr.schemas(),
        )
        needs_compact = self.assembly.compact_mgr.is_need_compact(
            ctx.messages,
            ctx.prompt,
            self.deps.tools_mgr.schemas(),
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
                    self.spec.agent_type,
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
                    self.spec.agent_type,
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
            source=self.spec.agent_type,
            content="auto compact",
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))
        result = await self.assembly.compact_mgr.compact_history(
            ctx.messages,
            bootstrap_message_count=getattr(
                self, "_initial_context_message_count", 0,
            ),
        )
        self._replace_messages(ctx.messages, result.messages)
        await self._commit_session()
        ctx.loaded_skills.clear()
        ctx.auto_compact_summarized_message_count = result.summarized_message_count
        ctx.auto_compact_has_summary = bool(result.summary.strip())
        return AgentState.CHECK_COMPACT

    def _base_reasoning_effort(self) -> str:
        """返回本 agent 无长度降档时实际使用的推理力度档位。

        Returns:
            自身 manifest 声明的 reasoning_effort，未声明时回退 Provider 类默认档位。
        """
        return self.reasoning_effort or self.assembly.llm.reasoning_effort

    async def _on_llm_call(self, ctx: RunContext) -> AgentState:
        """调用 LLM 并保存响应。

        Args:
            ctx: 当前运行上下文。

        Returns:
            PROCESS_RESPONSE。

        Raises:
            LLMCallError: 调用不可继续时交由单轮状态机边界收口。
        """
        ctx.llm_call_count += 1
        state = self._session_state()
        # 工作期间排队的用户输入必须在回滚点之前并入：中断回滚只丢弃本次调用
        # 新增的临时消息，用户说过的话要留在模型历史里。
        await self._adopt_queued_user_inputs()
        ctx.llm_call_start_idx = len(ctx.messages)
        ctx.llm_call_context_start = (
            len(state.context_ids) if state is not None else 0
        )
        self._append_pending_framework_message(ctx)
        self._replace_messages(
            ctx.messages,
            self.assembly.llm.normalize_messages(ctx.messages),
            preserve_positions=True,
        )
        ctx.response = await self.assembly.llm.chat(
            prompt=ctx.prompt,
            messages=ctx.messages,
            tools=self.deps.tools_mgr.schemas(),
            caller_agent_type=self.spec.agent_type,
            caller_uuid=str(self.uuid),
            enable_thinking=self.spec.enable_thinking,
            reasoning_effort_override=ctx.length_effort_override or self.reasoning_effort,
            phase=self.mode.value,
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
        self._settle_stream_views()
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
        ctx.length_effort_override = None
        ctx.pending_framework_instructions.clear()
        self._append_message(
            ctx.messages,
            response.assistant_message,
            correlation_id=response.call_id or None,
            kind="assistant",
        )
        if response.tool_calls:
            await self._commit_session()
            return AgentState.EXECUTE_TOOLS
        await self._commit_session()
        return AgentState.CHECK_STOP

    async def _on_length_retry(self, ctx: RunContext) -> AgentState:
        """按截断所处阶段选择续写或丢弃重生成两类恢复策略。

        工具/正文截断走续写路径：保留已完成的残片、追加续写指令后重试。思考/未知
        阶段截断走重生成路径：丢弃整条不完整响应（不向 ctx.messages 追加任何内容），
        改为临时降低推理力度重生成；无更低档位时附加一次性压缩推理指令兜底。

        Args:
            ctx: 当前运行上下文；截断响应取自 ctx.response。续写路径把恢复消息写入
                ctx.messages，重生成路径仅更新降档/压缩瞬态字段而不改动消息。

        Returns:
            未达到恢复上限时返回 LLM_CALL，达到上限时返回 LLM_FAILURE。
        """
        response = ctx.response
        kind = response.truncation_kind or classify_truncation(response)
        assistant_message = response.assistant_message or {}
        ctx.response_recovery_response_count += 1
        ctx.pause_turn_message_idx = None
        if ctx.response_recovery_start_idx is None:
            ctx.response_recovery_start_idx = len(ctx.messages)

        regenerate = kind in (TruncationKind.THINKING.value, TruncationKind.UNKNOWN.value)
        is_tool_call = kind == TruncationKind.TOOL_CALL.value

        # 续写路径保留残片作为历史；重生成路径丢弃整条响应，不追加任何消息。
        if not regenerate:
            if is_tool_call:
                if response.content:
                    self._append_message(
                        ctx.messages,
                        {"role": "assistant", "content": response.content},
                        correlation_id=response.call_id or None,
                        kind="assistant",
                    )
            else:
                self._append_message(
                    ctx.messages,
                    response.assistant_message
                    or {"role": "assistant", "content": response.content or None},
                    correlation_id=response.call_id or None,
                    kind="assistant",
                )

        if ctx.length_recoveries >= ctx.max_length_recoveries:
            return await self._fail_response_recovery(
                ctx,
                message=self._length_failure_message(kind),
                partial=bool(
                    response.has_partial_data
                    or response.content
                    or is_tool_call
                    or assistant_message.get("reasoning_content")
                ),
                tool_fragment_state="partial" if is_tool_call else "none",
                original_exception_type=(
                    "ThinkingOutputLengthLimit" if regenerate else "OutputLengthLimit"
                ),
            )

        ctx.length_recoveries += 1

        if regenerate:
            current_effort = ctx.length_effort_override or self._base_reasoning_effort()
            lower = self.assembly.llm.next_lower_effort(current_effort)
            if lower:
                ctx.length_effort_override = lower
                strategy, effort = "regenerate-lower-effort", lower
            else:
                ctx.pending_framework_instructions.append(_COMPRESS_REASONING_INSTRUCTION)
                strategy, effort = "regenerate-compress", current_effort
        else:
            if is_tool_call:
                retry_instruction = (
                    "上一次工具调用因输出长度限制而不完整，已丢弃且未执行。"
                    "请重新生成完整、有效的工具调用；若参数较长，请拆分为多个较小调用。"
                    "写入大文件时请使用现有的分块能力。"
                )
            else:
                retry_instruction = "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。"
            ctx.pending_framework_instructions.append(retry_instruction)
            strategy = "continue"
            effort = ctx.length_effort_override or self._base_reasoning_effort()

        await self._emit_length_retrying(
            ctx,
            truncation_kind=kind,
            strategy=strategy,
            effort=effort,
        )
        return AgentState.LLM_CALL

    @staticmethod
    def _length_failure_message(kind: str) -> str:
        """返回与截断阶段匹配的输出上限失败文案。

        Args:
            kind: TruncationKind 之一的字符串值。

        Returns:
            面向用户的单句安全错误摘要。
        """
        if kind == TruncationKind.TOOL_CALL.value:
            return (
                "模型工具调用连续被截断，未执行不完整调用，"
                "已达到自动恢复上限。请缩小参数范围后重试。"
            )
        if kind in (TruncationKind.THINKING.value, TruncationKind.UNKNOWN.value):
            return (
                "模型在思考阶段连续达到输出上限，降低推理力度后仍无法完成，"
                "已达到自动恢复上限。请缩小任务范围或改用输出上限更高的模型后重试。"
            )
        return "模型输出连续被截断，已达到自动续写恢复上限。请缩小输出范围后重试。"

    async def _emit_length_retrying(
        self,
        ctx: RunContext,
        *,
        truncation_kind: str,
        strategy: str,
        effort: str,
    ) -> None:
        """发出一次长度截断自动恢复的进度事件。

        Args:
            ctx: 当前运行上下文，用于读取长度恢复计数。
            truncation_kind: 本次截断所处阶段的分类字符串。
            strategy: 采取的恢复策略（continue / regenerate-lower-effort / regenerate-compress）。
            effort: 本次恢复调用将使用的推理力度档位。

        Returns:
            None。
        """
        event_bus = getattr(getattr(self, "deps", None), "event_bus", None)
        if event_bus is None:
            return
        caller_agent_type, caller_uuid = caller_identity(self)
        await emit_telemetry_safely(event_bus, LLMLengthRetrying(
            timestamp=time.time(),
            source=self.spec.agent_type,
            truncation_kind=truncation_kind,
            call_id=getattr(ctx.response, "call_id", "") if ctx.response is not None else "",
            strategy=strategy,
            effort=effort,
            attempt=ctx.length_recoveries,
            max_attempts=ctx.max_length_recoveries,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

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

        limit = self.assembly.llm.protocol_continuation_limit("pause_turn")
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
            self._append_message(ctx.messages, carrier)
        else:
            self._set_message(ctx.messages, pause_message_idx, carrier)
        ctx.pause_turn_continuations += 1
        self._replace_messages(
            ctx.messages,
            self.assembly.llm.normalize_messages(ctx.messages),
            preserve_positions=True,
        )
        if ctx.messages and ctx.messages[-1].get("role") == "assistant":
            ctx.pause_turn_message_idx = len(ctx.messages) - 1
        else:
            ctx.pause_turn_message_idx = None
        return AgentState.LLM_CALL

    async def _on_execute_tools(self, ctx: RunContext) -> AgentState:
        """并发独立读取；修改和交互形成屏障。取消保留已完成调用结果。"""
        ctx.has_tool_calls = True
        calls = list(ctx.response.tool_calls.values())
        completed = {}
        started: dict[str, float] = {}
        end_turn = False

        async def run_one(tc):
            name, ident = tc["name"], tc["id"]
            started[ident] = time.monotonic()
            try:
                try:
                    args = json.loads(tc["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须为 JSON 对象")
                except (ValueError, TypeError):
                    completed[ident] = await self.deps.tools_mgr.execute(name, tc["arguments"], current_tool_call_id=ident, deps=self.deps, agent=self, run_context=ctx)
                    return
                completed[ident] = await self.deps.tools_mgr.execute(name, args, current_tool_call_id=ident, deps=self.deps, agent=self, run_context=ctx)
            except asyncio.CancelledError:
                # 已派发的调用被取消：给模型一条带时长的中止结果，避免它以为工具从未运行。
                completed[ident] = self._aborted_tool_result(started[ident])
                raise

        async def parallel(tc):
            tool = self.deps.tools_mgr.get(tc["name"])
            if tool is None:
                return False
            if tc["name"] != "exec_command":
                return tool.parallel
            # Plan 沙箱不能写工作区；实际权限仍由工具入口独立授权。
            return self.mode is RunMode.PLAN

        async def flush(batch):
            tasks = [asyncio.create_task(run_one(tc)) for tc in batch]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        try:
            batch = []
            for tc in calls:
                if await parallel(tc):
                    batch.append(tc)
                else:
                    await flush(batch)
                    batch = []
                    await run_one(tc)
                    if completed[tc["id"]].end_turn:
                        break
            await flush(batch)
        finally:
            for tc in calls:
                result = completed.get(tc["id"]) or self._not_executed_tool_result()
                end_turn = end_turn or result.end_turn
                self._append_message(ctx.messages, {"role": "tool", "tool_call_id": tc["id"], "content": str(result)}, correlation_id=tc["id"], kind="tool")
        self.assembly.reminder_mgr.notify_tool_round([tc["name"] for tc in calls if tc["id"] in completed])
        await self._commit_session()
        return AgentState.DONE if end_turn else AgentState.POST_ROUND

    async def _on_check_stop(self, ctx: RunContext) -> AgentState:
        if self.deps.hooks_mgr is not None and not ctx.stop_hook_used:
            stop_hook = await self.deps.hooks_mgr.run_event(
                "Stop",
                ctx.final_text,
                {"final_text": ctx.final_text},
                session_id=self.deps.session.id,
                agent_id=str(self.uuid),
                agent_type=self.spec.agent_type,
            )
            if stop_hook.blocked:
                ctx.stop_hook_used = True
                reason = stop_hook.block_reason or "Stop hook blocked"
                self._append_message(ctx.messages, {
                    "role": "user",
                    "content": render_external_context(
                        ExternalContextItem("Stop hook", reason)
                    ),
                }, kind="external_context")
                return AgentState.CHECK_COMPACT
        return AgentState.DONE

    async def _on_post_round(self, ctx: RunContext) -> AgentState:
        self.assembly.reminder_mgr.queue_post_round(self.mode, self.spec.is_subagent)
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
        summary_start_idx = len(ctx.messages)
        ctx.pending_framework_instructions.append(
            "由于对话上下文过长且多次压缩仍无法继续，请基于当前已完成的工作总结："
            "1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。"
        )
        self._append_pending_framework_message(ctx)
        summary_messages = self.assembly.llm.normalize_messages(ctx.messages)
        try:
            response = await self.assembly.llm.chat(
                prompt=ctx.prompt,
                messages=summary_messages,
                tools=[],
                caller_agent_type=self.spec.agent_type,
                caller_uuid=str(self.uuid),
                enable_thinking=False,
            )
        except LLMCallError:
            self._truncate_messages(ctx.messages, summary_start_idx)
            raise
        if response.content:
            ctx.final_text = response.content
        self._replace_messages(ctx.messages, summary_messages)
        self._append_message(
            ctx.messages,
            response.assistant_message,
            correlation_id=response.call_id or None,
            kind="assistant",
        )
        await self._commit_session()
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

    @staticmethod
    def _is_injectable_user_input(text: str) -> bool:
        """判断排队文本能否中途并入：命令与退出词必须留给输入窗口解析。"""
        return parse_command(text) is None and text.strip().lower() not in ("exit", "quit")

    async def _adopt_queued_user_inputs(self) -> int:
        """在 chat 之前把排队输入逐条并入对话（仅主 agent）。

        命令与退出词留在队列里，由 ``REQUEST_INPUT`` 正常解析；子 agent 不消费
        用户排队输入，避免把用户的话注入到子 agent 的上下文。

        Returns:
            本次并入的条数。
        """
        pending = self._queued_inputs()
        if self.spec.is_subagent or not pending:
            return 0
        adopted = 0
        while pending and self._is_injectable_user_input(pending[0]):
            await self._adopt_user_input(pending.popleft())
            adopted += 1
        return adopted

    async def _adopt_user_input(self, text: str) -> None:
        """把一条排队文本并入模型历史与会话状态，并通知 UI 展示。"""
        data_guard = getattr(self.deps, "data_guard", None)
        content = str(data_guard.redact(text)) if data_guard is not None else text
        state = self._session_state()
        record_id = state.append_user(content) if state is not None else None
        self._append_message(
            self.history,
            {"role": "user", "content": content},
            record_id=record_id,
            kind="user",
        )
        await self._emit_user_input_adopted(content)
        await self._commit_session()

    async def _emit_user_input_adopted(self, text: str) -> None:
        """通知 UI：这条用户输入已并入对话，可在正确位置展示。"""
        event_bus = getattr(self.deps, "event_bus", None)
        if event_bus is None:
            return
        caller_agent_type, caller_uuid = caller_identity(self)
        await event_bus.emit(UserInputAdopted(
            timestamp=time.time(),
            source=self.spec.agent_type,
            text=text,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    def _append_initial_context_messages(self) -> None:
        """在首条真实输入之前追加一次外部上下文，不污染固定 system。"""
        if self._initial_context_added:
            return
        messages = self.assembly.prompt_mgr.build_initial_context_messages()
        for message in messages:
            self._append_message(self.history, message, kind="external_context")
        self._initial_context_message_count = len(messages)
        self._initial_context_added = True

    def _append_pending_framework_message(self, ctx: RunContext) -> None:
        """在 chat 前把模式变化和框架提醒合并成最多一条 developer 消息。"""
        sections: list[str] = []
        mode = self.mode
        prompt_mgr = self.assembly.prompt_mgr
        if self._last_capability_mode is not mode:
            capability_text = prompt_mgr.build_capability_instructions(mode)
            if capability_text:
                sections.append(capability_text)
            self._last_capability_mode = mode
        if not self._manager_guidance_injected:
            manager_text = prompt_mgr.build_manager_instructions()
            if manager_text:
                sections.append(manager_text)
            self._manager_guidance_injected = True

        if self._last_injected_mode is not mode:
            mode_text = prompt_mgr.build_mode_instructions(mode)
            if mode_text:
                sections.append(render_prompt_tag(
                    PromptTag.COLLABORATION_MODE, mode_text,
                ))
            self._last_injected_mode = mode

        reminders = list(ctx.pending_framework_instructions)
        ctx.pending_framework_instructions.clear()
        reminders.extend(self.assembly.reminder_mgr.pop_pending())
        seen: set[str] = set()
        for reminder in reminders:
            if reminder and reminder not in seen:
                sections.append(render_prompt_tag(PromptTag.REMINDER, reminder))
                seen.add(reminder)

        if sections:
            self._append_message(
                ctx.messages,
                {"role": "developer", "content": "\n\n".join(sections)},
                kind="framework",
            )

    def _safe_history_value(self, value: Any) -> Any:
        data_guard = getattr(getattr(self, "deps", None), "data_guard", None)
        return data_guard.redact(value) if data_guard is not None else value

    def _normalize_stored_text(self, message: dict) -> dict:
        """把消息正文规范成 provider 之后会写入的形式。

        归一化会改写字符串正文的 provider（例如 DeepSeek 去掉首尾空白）若先落盘
        原样、下一轮再把归一化结果回写，同一段正文会在日志里留下两份。这里在首次
        写入前就使用同一规则，模型看到的字节不变。

        Args:
            message: 待写入的模型消息。

        Returns:
            正文已规范化的消息；无需改动时返回原对象。
        """
        content = message.get("content")
        if not isinstance(content, str) or not content:
            return message
        if "_response_output" in message or "_anthropic_content" in message:
            return message
        llm = getattr(getattr(self, "assembly", None), "llm", None)
        normalize = getattr(llm, "normalize_stored_text", None)
        if normalize is None:
            return message
        normalized = normalize(content)
        if not isinstance(normalized, str) or normalized == content:
            return message
        return {**message, "content": normalized}

    def _session_state(self) -> "SessionState | None":
        if getattr(self, "is_subagent", False):
            return None
        session = getattr(getattr(self, "deps", None), "session", None)
        return getattr(session, "state", None)

    def _append_message(
        self,
        messages: list[dict],
        message: dict,
        *,
        record_id: str | None = None,
        correlation_id: str | None = None,
        kind: str = "internal",
    ) -> None:
        safe_message = self._normalize_stored_text(self._safe_history_value(message))
        state = self._session_state() if messages is getattr(self, "history", None) else None
        if state is not None:
            state.bind_model_message(
                safe_message,
                record_id=record_id,
                correlation_id=correlation_id,
                kind=kind,
            )
        messages.append(safe_message)

    def _replace_messages(
        self,
        messages: list[dict],
        new_messages: list[dict],
        *,
        preserve_positions: bool = False,
    ) -> None:
        safe_messages = self._safe_history_value(new_messages)
        state = self._session_state() if messages is getattr(self, "history", None) else None
        if state is not None:
            state.replace_context(safe_messages, preserve_positions=preserve_positions)
        messages[:] = safe_messages

    def _truncate_messages(self, messages: list[dict], length: int) -> None:
        state = self._session_state() if messages is getattr(self, "history", None) else None
        if state is not None:
            state.truncate_context(length)
        del messages[length:]

    def _set_message(self, messages: list[dict], index: int, message: dict) -> None:
        safe_message = self._normalize_stored_text(self._safe_history_value(message))
        state = self._session_state() if messages is getattr(self, "history", None) else None
        if state is not None:
            state.set_context_message(index, safe_message)
        messages[index] = safe_message

    async def _commit_session(self, user_input: str = "") -> None:
        """把自上次提交以来的会话状态增量追加到日志（仅主 agent，子 agent 跳过）。

        增量在事件循环线程取出，写盘与元数据更新在工作线程完成；首次提交时写入
        is_new=True 并把用户首条消息设为 topic。无变更且无需更新元数据时零写入。

        Args:
            user_input: 用户本轮原始输入，首次调用时作为会话主题。
        """
        target = self._session_target()
        if target is None:
            return
        session_mgr, session_id, state = target
        async with self.deps.session.commit_lock:
            # 提交前按整段复核一次流式正文，保证内存、显示与磁盘是同一份脱敏文本。
            self._settle_stream_views()
            commit = state.prepare_commit()
            if commit is None and not user_input:
                return
            has_history = bool(self.history)
            mode = self.mode

            def save() -> bool:
                success = commit is None or session_mgr.append_batch(
                    session_id, commit.payload, commit.resync,
                )
                if user_input and has_history:
                    is_new = session_mgr.get_metadata(session_id) is None
                    session_mgr.save_metadata(
                        session_id,
                        is_new=is_new,
                        topic=user_input if is_new else "",
                        mode=mode,
                    )
                return success

            success = await asyncio.to_thread(save)
            if commit is not None and success:
                state.ack_commit(commit)
            elif commit is not None:
                state.requeue_commit(commit)

    def _session_target(self) -> tuple[Any, str, "SessionState"] | None:
        """返回可提交的 (session_mgr, session_id, state)。

        未接线完整的 Agent（子 agent、最小化测试实例或缺少会话管理器）返回 None，
        使提交点在所有路径上都是安全的空操作。
        """
        spec = getattr(self, "spec", None)
        if spec is None or spec.is_subagent:
            return None
        session = getattr(getattr(self, "deps", None), "session", None)
        session_mgr = getattr(getattr(self, "deps", None), "session_mgr", None)
        state = getattr(session, "state", None)
        session_id = getattr(session, "id", "")
        if session_mgr is None or state is None or not session_id:
            return None
        return session_mgr, session_id, state

    async def _emit_state_changed(self, from_state: AgentState, to_state: AgentState) -> None:
        if self.deps.event_bus is None:
            return
        await self.deps.event_bus.emit(AgentStateChanged(
            timestamp=time.time(),
            source=self.spec.agent_type,
            agent_id=str(self.uuid),
            agent_type=self.spec.agent_type,
            from_state=from_state.value,
            to_state=to_state.value,
        ))
