from __future__ import annotations

import asyncio, json, logging, time, uuid
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict
from src.tools import ToolDict
from src.events.types import AgentStateChanged, CompactDelta
from src.agent.states import AgentState, RunContext
from src.mgr import FileMgr, TodoManager, CompactMgr, CompactResult, PromptMgr, SkillMgr, SubAgentMgr

logger = logging.getLogger(__name__)

class AgentDeps(BaseModel):
    """外部依赖（进程级全局对象）。

    /clear 时通过 hasattr(mgr, "reload") 判断并调用，
    仅在管理器有运行时可变状态需要重置时才实现 reload() 方法。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    llm_mgr: Any = None  # LLMMgr
    ui: Any = None  # UserInterface
    event_bus: Any = None  # EventBus
    tools_mgr: Any = None  # ToolsMgr
    permission_mgr: Any = None  # PermissionManager
    config_mgr: Any = None  # ConfigManager
    memory_mgr: Any = None  # MemoryMgr
    hooks_mgr: Any = None  # HooksMgr
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
    _handlers: dict[AgentState, Callable] = field(init=False, repr=False)

    def __post_init__(self):
        self.uuid = uuid.uuid4()
        self.llm = self.deps.llm_mgr.get(self.model)
        self._tools_schemas = self.deps.tools_mgr.get_schemas(self.tools)
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

    async def run(self, input: str) -> str:
        self.history.append({"role": "user", "content": input})
        ctx = RunContext(
            messages=self.history,
            round_start_idx=len(self.history),
        )
        try:
            state = AgentState.CHECK_COMPACT
            while state != AgentState.DONE:
                prev = state
                state = await self._handlers[state](ctx)
                await self._emit_state_changed(prev, state)

            if not ctx.has_tool_calls:
                self.llm.clear_reasoning_content(self.history[ctx.round_start_idx:])
            return ctx.final_text
        except (asyncio.CancelledError, KeyboardInterrupt):
            del self.history[ctx.round_start_idx:]
            raise

    # ---- state handlers ----

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
