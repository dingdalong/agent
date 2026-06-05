from __future__ import annotations

import asyncio, json, logging, time, uuid
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING
from src.tools import ToolDict
from src.events.types import AgentStateChanged, CompactDelta
from src.agent.states import AgentState, RunContext, RunResult, parse_command
from src.events import NoEventSubscribers
from src.mgr import FileMgr, TodoManager, CompactMgr, CompactResult, PromptMgr, SkillMgr, SubAgentMgr

if TYPE_CHECKING:
    from src.mgr.llm_mgr import LLMMgr
    from src.interfaces.base import UserInterface
    from src.events.bus import EventBus
    from src.mgr.tools_mgr import ToolsMgr
    from src.mgr.permission_mgr import PermissionManager
    from src.mgr.config_mgr import ConfigManager
    from src.mgr.memory_mgr import MemoryMgr
    from src.mgr.hooks_mgr import HooksMgr
    from src.mgr.plan_mgr import PlanMgr

logger = logging.getLogger(__name__)

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
    session_context: list[str] = field(default_factory=list)
    session_id: str = ""

@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        uuid: 唯一类型标识。
        agent_type: agent类型
        description: 一句话描述
    """

    uuid: UUID = field(init=False)
    agent_type: str
    description: str
    deps: AgentDeps = field(repr=False)
    role_prompt: str | None = field(default=None)
    tools: set[str] | None = field(default=None)
    is_subagent: bool = field(default=False)
    memory: str | None = field(default="project")
    model: str | None = field(default=None)
    history: list[dict] = field(init=False, default_factory=list)
    _tools_schemas: list[ToolDict] = field(init=False)
    _todo_mgr: TodoManager = field(init=False, default_factory=TodoManager, repr=False)
    _compact_mgr: CompactMgr = field(init=False, repr=False)
    _file_mgr: FileMgr = field(init=False, repr=False)
    _skill_mgr: SkillMgr = field(init=False, repr=False)
    _subagent_mgr: SubAgentMgr = field(init=False, repr=False)
    _prompt_mgr: PromptMgr = field(init=False, repr=False)
    _pending_input: str = field(init=False, default="")
    _handlers: dict[AgentState, Callable] = field(init=False, repr=False)

    def __post_init__(self):
        self.uuid = uuid.uuid4()
        self.llm = self.deps.llm_mgr.get(self.model)
        self.refresh_tools_schemas()
        compact_cfg = self.deps.config_mgr.get_config("compact")
        context_limit = self.llm.context_limit
        self._compact_mgr = CompactMgr(
            llm=self.llm,
            auto_compact_size=int(context_limit * compact_cfg["auto_compact_rate"]),
            keep_recent_user_turns=compact_cfg.get("keep_recent_user_turns", 3),
            recent_messages_token_limit=int(context_limit * compact_cfg.get("keep_recent_messages_token_rate", 0.25)),
        )
        workspace = Path.cwd() / "workspace"
        self._file_mgr = FileMgr(workspace, self.deps)
        self._skill_mgr = SkillMgr(workspace)
        self._subagent_mgr = SubAgentMgr(workspace, self.deps)
        self._prompt_mgr = PromptMgr(agent=self, model=self.llm.model, workdir=workspace, role_prompt=self.role_prompt)
        self._handlers = {
            AgentState.REQUEST_INPUT:    self._on_request_input,
            AgentState.CHECK_COMPACT:    self._on_check_compact,
            AgentState.COMPACT:          self._on_compact,
            AgentState.LLM_CALL:         self._on_llm_call,
            AgentState.PROCESS_RESPONSE: self._on_process_response,
            AgentState.LENGTH_RETRY:     self._on_length_retry,
            AgentState.EXECUTE_TOOLS:    self._on_execute_tools,
            AgentState.CHECK_STOP:       self._on_check_stop,
            AgentState.POST_ROUND:       self._on_post_round,
            AgentState.SUMMARIZE_EXIT:   self._on_summarize_exit,
            AgentState.CONTEXT_OVERFLOW: self._on_context_overflow,
        }

    def refresh_tools_schemas(self) -> None:
        self._tools_schemas = self.deps.tools_mgr.get_schemas(
            self.tools,
            permission_mgr=self.deps.permission_mgr,
        )

    async def run(self, input: str | None = None) -> RunResult:
        """运行一轮 agent 对话。

        Args:
            input: 用户输入文本。为 None 时从 REQUEST_INPUT 状态开始，
                   通过 event_bus 收集用户输入；不为 None 时直接从
                   CHECK_COMPACT 开始（子智能体路径）。

        Returns:
            RunResult，包含最终文本、斜杠命令、退出请求等信息。
        """
        ctx = RunContext(
            messages=self.history,
            round_start_idx=len(self.history),
        )

        if input is not None:
            plan_mgr = self.deps.plan_mgr
            if plan_mgr is not None and self.deps.permission_mgr is not None:
                plan_instr = plan_mgr.build_instructions(self.deps.permission_mgr)
                if plan_instr:
                    input = f"{plan_instr}\n\n{input}"
            self.history.append({"role": "user", "content": input})
            ctx.round_start_idx = len(self.history)
            ctx.user_input = input
            state = AgentState.CHECK_COMPACT
        else:
            state = AgentState.REQUEST_INPUT

        try:
            while state != AgentState.DONE:
                prev = state
                state = await self._handlers[state](ctx)
                await self._emit_state_changed(prev, state)

            if not ctx.command and not ctx.exit_requested and not ctx.has_tool_calls:
                self.llm.clear_reasoning_content(self.history[ctx.round_start_idx:])

            return RunResult(
                final_text=ctx.final_text,
                command=ctx.command,
                exit_requested=ctx.exit_requested,
                user_input=ctx.user_input,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            del self.history[ctx.round_start_idx:]
            self._pending_input = ctx.user_input
            raise

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
            ctx.command = cmd
            return AgentState.DONE

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
                await self.deps.event_bus.request_output(f"{reason}\n")
                return AgentState.REQUEST_INPUT
            if hook_result.additional_context:
                user_input = user_input + "\n\n" + "\n\n".join(
                    str(item) for item in hook_result.additional_context
                )

        plan_mgr = self.deps.plan_mgr
        if plan_mgr is not None and self.deps.permission_mgr is not None:
            plan_instr = plan_mgr.build_instructions(self.deps.permission_mgr)
            if plan_instr:
                user_input = f"{plan_instr}\n\n{user_input}"

        self.history.append({"role": "user", "content": user_input})
        ctx.round_start_idx = len(self.history)

        return AgentState.CHECK_COMPACT

    async def _on_check_compact(self, ctx: RunContext) -> AgentState:
        ctx.prompt = self._prompt_mgr.build()
        if not self._compact_mgr.is_need_compact(ctx.messages, ctx.prompt, self._tools_schemas):
            ctx.compact_streak = 0
            return AgentState.LLM_CALL
        ctx.compact_streak += 1
        if ctx.compact_streak > ctx.max_compact_streak:
            logger.warning("连续 %d 次 compact 后仍需压缩", ctx.compact_streak - 1)
            return AgentState.SUMMARIZE_EXIT
        return AgentState.COMPACT

    async def _on_compact(self, ctx: RunContext) -> AgentState:
        await self.deps.event_bus.emit(CompactDelta(
            timestamp=time.time(),
            source=self.agent_type,
            content="auto compact",
        ))
        result = await self._compact_mgr.compact_history(ctx.messages)
        ctx.messages[:] = result.messages
        if result.transcript_path:
            await self.deps.event_bus.request_output(f"[transcript saved: {result.transcript_path}]\n")
        return AgentState.LLM_CALL

    async def _on_llm_call(self, ctx: RunContext) -> AgentState:
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        try:
            ctx.response = await self.llm.chat(
                prompt=ctx.prompt,
                messages=ctx.messages,
                tools=self._tools_schemas,
                caller_agent_type=self.agent_type,
                caller_uuid=str(self.uuid),
            )
        except Exception as exc:
            if self.llm.is_context_too_long_error(exc):
                return AgentState.CONTEXT_OVERFLOW
            raise
        return AgentState.PROCESS_RESPONSE

    async def _on_process_response(self, ctx: RunContext) -> AgentState:
        response = ctx.response
        if response.content:
            ctx.final_text = response.content

        if response.finish_reason == "length":
            return AgentState.LENGTH_RETRY

        ctx.messages.append(response.assistant_message)

        if response.tool_calls:
            return AgentState.EXECUTE_TOOLS
        return AgentState.CHECK_STOP

    async def _on_length_retry(self, ctx: RunContext) -> AgentState:
        response = ctx.response
        ctx.messages.append(
            response.assistant_message
            or {"role": "assistant", "content": response.content or None}
        )
        if ctx.length_recoveries >= ctx.max_length_recoveries:
            ctx.final_text = "错误：模型输出连续被截断，已达到自动续写恢复上限。请缩小输出范围后重试。"
            ctx.messages.append({"role": "assistant", "content": ctx.final_text})
            return AgentState.DONE

        ctx.length_recoveries += 1
        ctx.messages.append({"role": "user", "content": "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。"})
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        return AgentState.LLM_CALL

    async def _on_execute_tools(self, ctx: RunContext) -> AgentState:
        ctx.has_tool_calls = True
        used_todo = False
        ctx.manual_compact = False
        ctx.compact_focus = None

        for tc in ctx.response.tool_calls.values():
            tool_name = tc["name"]
            tool_call_id = tc["id"]

            if self.tools is not None and tool_name not in self.tools:
                ctx.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"错误：未知工具 '{tool_name}'",
                })
                continue

            if tool_name == "todo_write":
                used_todo = True
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

            ctx.messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_text,
            })

        ctx.rounds_without_todo = 0 if used_todo else ctx.rounds_without_todo + 1
        if self.deps.plan_mgr is not None:
            self.deps.plan_mgr.notify_round()
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
        if self._todo_mgr.has_open_items() and ctx.rounds_without_todo >= 3:
            ctx.messages.append({
                "role": "user",
                "content": [{"type": "text", "text": "<reminder>更新你的待办事项。</reminder>"}],
            })

        if self.deps.plan_mgr is not None and self.deps.permission_mgr is not None:
            plan_msg = self.deps.plan_mgr.pop_pending_message(self.deps.permission_mgr)
            if plan_msg:
                ctx.messages.append({
                    "role": "user",
                    "content": f"<plan-mode>{plan_msg}</plan-mode>",
                })

        if ctx.manual_compact:
            await self.deps.event_bus.emit(CompactDelta(
                timestamp=time.time(),
                source=self.agent_type,
                content="llm manual",
            ))
            result = await self._compact_mgr.compact_history(
                ctx.messages, focus=ctx.compact_focus,
            )
            ctx.messages[:] = result.messages
            if result.transcript_path:
                await self.deps.event_bus.request_output(f"[transcript saved: {result.transcript_path}]\n")

        return AgentState.CHECK_COMPACT

    async def _on_summarize_exit(self, ctx: RunContext) -> AgentState:
        ctx.messages.append({"role": "user", "content": "由于对话上下文过长且多次压缩仍无法继续，请你基于当前已完成的工作做一个总结："
            "1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。"})
        ctx.messages[:] = self.llm.normalize_messages(ctx.messages)
        try:
            response = await self.llm.chat(
                prompt=ctx.prompt,
                messages=ctx.messages,
                tools=[],
                caller_agent_type=self.agent_type,
                caller_uuid=str(self.uuid),
            )
        except Exception as exc:
            if self.llm.is_context_too_long_error(exc):
                ctx.final_text = "错误：上下文过长，已多次压缩仍无法继续。请缩小任务范围或重新开始较短的会话。"
                ctx.messages.append({"role": "assistant", "content": ctx.final_text})
                return AgentState.DONE
            raise
        if response.content:
            ctx.final_text = response.content
        ctx.messages.append(response.assistant_message)
        return AgentState.DONE

    async def _on_context_overflow(self, ctx: RunContext) -> AgentState:
        ctx.final_text = "错误：上下文过长，已多次压缩仍无法继续。请缩小任务范围或重新开始较短的会话。"
        ctx.messages.append({"role": "assistant", "content": ctx.final_text})
        return AgentState.DONE

    # ---- helpers ----

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
