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
from src.mgr import FileMgr, TaskManager, CompactMgr, CompactResult, PromptMgr, SkillMgr, SubAgentMgr, ReminderMgr

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
    from src.mgr.plugin_mgr import PluginMgr
    from src.mgr.session_mgr import SessionMgr

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
    plugin_mgr: PluginMgr | None = None
    session_mgr: SessionMgr | None = None
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
    _task_mgr: TaskManager = field(init=False, repr=False)
    _compact_mgr: CompactMgr = field(init=False, repr=False)
    _file_mgr: FileMgr = field(init=False, repr=False)
    _skill_mgr: SkillMgr = field(init=False, repr=False)
    _subagent_mgr: SubAgentMgr = field(init=False, repr=False)
    _prompt_mgr: PromptMgr = field(init=False, repr=False)
    _reminder_mgr: ReminderMgr = field(init=False, repr=False)
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
            workdir=self.deps.workdir,
            auto_compact_size=int(context_limit * compact_cfg["auto_compact_rate"]),
            keep_recent_user_turns=compact_cfg.get("keep_recent_user_turns", 3),
            recent_messages_token_limit=int(context_limit * compact_cfg.get("keep_recent_messages_token_rate", 0.25)),
        )
        workdir = self.deps.workdir
        self._file_mgr = FileMgr(workdir, self.deps)
        self._skill_mgr = SkillMgr(workdir, global_dir=self.deps.global_dir, plugin_mgr=self.deps.plugin_mgr)
        self._subagent_mgr = SubAgentMgr(workdir, self.deps, global_dir=self.deps.global_dir)
        self._prompt_mgr = PromptMgr(agent=self, model=self.llm.model, workdir=workdir, global_dir=self.deps.global_dir, role_prompt=self.role_prompt)
        # 主 agent：持久化到磁盘；子 agent：纯内存模式，独立实例。
        tasks_dir = None
        if not self.is_subagent and self.deps.global_dir and self.deps.session_id:
            tasks_dir = self.deps.global_dir / "tasks" / self.deps.session_id
        self._task_mgr = TaskManager(tasks_dir=tasks_dir)
        self._reminder_mgr = ReminderMgr()
        self._reminder_mgr.register(self._task_mgr)
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
        """刷新工具 schema 列表。"""
        self._tools_schemas = self.deps.tools_mgr.get_schemas(
            self.tools,
            permission_mgr=self.deps.permission_mgr,
        )

    def set_permission_mode(self, mode) -> bool:
        """切换权限模式，处理计划模式的特殊进入/退出逻辑。

        统一入口：/mode 命令、Shift+Tab 轮转、prompt_selection 菜单均通过此方法切换。

        Args:
            mode: 目标权限模式（PermissionMode 实例）。

        Returns:
            模式是否发生了变化。
        """
        from src.mgr.permission_mgr import PLAN_MODE
        permission_mgr = self.deps.permission_mgr
        plan_mgr = self.deps.plan_mgr

        if mode is PLAN_MODE and plan_mgr is not None:
            return plan_mgr.enter_mode(permission_mgr, self._reminder_mgr)

        if permission_mgr.mode is PLAN_MODE and plan_mgr is not None:
            plan_mgr.exit_mode(permission_mgr, self._reminder_mgr)

        return permission_mgr.set_mode(mode)

    async def run(self, input: str | None = None) -> RunResult:
        """运行 agent 对话。

        交互模式（input=None）下内部循环多轮对话，仅在 exit 或 /clear 时返回；
        子智能体模式（input 不为 None）下执行单轮后立即返回。

        Args:
            input: 用户输入文本。为 None 时从 REQUEST_INPUT 开始并循环；
                   不为 None 时从 CHECK_COMPACT 开始执行单轮（子智能体路径）。

        Returns:
            RunResult，包含最终文本、斜杠命令、退出请求等信息。
        """
        if input is not None:
            ctx = RunContext(messages=self.history, round_start_idx=len(self.history))
            turn_instr = self._reminder_mgr.build_turn_start_instructions(self.deps.permission_mgr)
            if turn_instr:
                input = f"{turn_instr}\n\n{input}"
            self.history.append({"role": "user", "content": input})
            ctx.round_start_idx = len(self.history)
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
            RunResult，包含本轮结果。
        """
        state = start_state
        try:
            while state != AgentState.DONE:
                prev = state
                state = await self._handlers[state](ctx)
                await self._emit_state_changed(prev, state)
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
                await self.deps.event_bus.request_output(f"{reason}\n")
                return AgentState.REQUEST_INPUT
            if hook_result.additional_context:
                user_input = user_input + "\n\n" + "\n\n".join(
                    str(item) for item in hook_result.additional_context
                )

        turn_instr = self._reminder_mgr.build_turn_start_instructions(self.deps.permission_mgr)
        if turn_instr:
            user_input = f"{turn_instr}\n\n{user_input}"

        self.history.append({"role": "user", "content": user_input})
        ctx.round_start_idx = len(self.history)

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
        self.deps.ui.on_system_state_changed()
        await self.deps.event_bus.request_output("已进入计划模式。\n")

    async def _handle_resume_command(self, cmd_args: list[str]) -> None:
        """处理 /resume 命令：列出或恢复历史会话。

        无参数时列出最近 10 个会话；有参数时按序号或 session_id 恢复。
        恢复时替换 self.history、切换 session_id、重建 TaskManager。

        Args:
            cmd_args: 命令参数列表，可为空、序号或 session_id。
        """
        session_mgr = self.deps.session_mgr
        if session_mgr is None:
            await self.deps.event_bus.request_output("会话管理器未初始化。\n")
            return

        sessions = session_mgr.list_sessions(limit=10)
        # 过滤掉当前会话
        sessions = [s for s in sessions if s.get("session_id") != self.deps.session_id]

        if not sessions:
            await self.deps.event_bus.request_output("没有可恢复的历史会话。\n")
            return

        if not cmd_args:
            # 列出会话
            lines = ["最近的历史会话：\n"]
            for i, s in enumerate(sessions, 1):
                updated = s.get("updated_at", "?")[:19].replace("T", " ")
                topic = s.get("topic", "")
                workdir = s.get("workdir", "")
                lines.append(f"  {i}. [{updated}] {workdir}\n     {topic}\n")
            lines.append("输入 /resume <序号> 恢复指定会话。\n")
            await self.deps.event_bus.request_output("\n".join(lines))
            return

        # 解析目标会话
        target_arg = cmd_args[0]
        target_session: dict | None = None

        # 尝试按序号解析
        try:
            idx = int(target_arg)
            if 1 <= idx <= len(sessions):
                target_session = sessions[idx - 1]
        except ValueError:
            pass

        # 尝试按 session_id 精确匹配或前缀匹配
        if target_session is None:
            for s in sessions:
                sid = s.get("session_id", "")
                if sid == target_arg or sid.startswith(target_arg):
                    target_session = s
                    break

        if target_session is None:
            await self.deps.event_bus.request_output(f"未找到匹配的会话: {target_arg}\n")
            return

        target_id = target_session["session_id"]

        # 加载历史
        messages = session_mgr.load_history(target_id)
        if not messages:
            await self.deps.event_bus.request_output(f"会话 {target_id[:8]}... 没有保存的对话历史。\n")
            return

        # 工作目录不一致时取消恢复
        saved_workdir = target_session.get("workdir", "")
        current_workdir = str(self.deps.workdir) if self.deps.workdir else ""
        if saved_workdir and current_workdir and saved_workdir != current_workdir:
            await self.deps.event_bus.request_output(
                f"无法恢复：原会话工作目录为 {saved_workdir}，当前为 {current_workdir}。\n"
                f"请在原工作目录下启动后再恢复该会话。\n"
            )
            return

        # 恢复会话状态
        self.history.clear()
        self.history.extend(messages)
        self.deps.session_id = target_id

        # 重建 TaskManager 指向恢复会话的 tasks 目录
        if self.deps.global_dir:
            tasks_dir = self.deps.global_dir / "tasks" / target_id
            from src.mgr import TaskManager
            self._task_mgr = TaskManager(tasks_dir=tasks_dir)
            self._reminder_mgr = ReminderMgr()
            self._reminder_mgr.register(self._task_mgr)

        # 恢复权限模式
        mode_info = ""
        saved_mode_value = target_session.get("permission_mode", "")
        if saved_mode_value and self.deps.permission_mgr is not None:
            from src.mgr.permission_mgr import PLAN_MODE, parse_permission_mode, DEFAULT_MODE
            if saved_mode_value == PLAN_MODE.value:
                # 恢复 plan 模式：先设置 pre_plan_mode，再通过 enter_mode 完整恢复
                pre_plan_value = target_session.get("pre_plan_mode", "")
                pre_plan = parse_permission_mode(pre_plan_value) if pre_plan_value else None
                self.deps.permission_mgr.mode = pre_plan or DEFAULT_MODE
                plan_mgr = self.deps.plan_mgr
                if plan_mgr is not None:
                    plan_mgr.enter_mode(self.deps.permission_mgr, self._reminder_mgr)
                mode_info = f"，权限模式: plan"
            else:
                mode = parse_permission_mode(saved_mode_value)
                if mode is not None:
                    self.set_permission_mode(mode)
                    mode_info = f"，权限模式: {mode.value}"
            self.refresh_tools_schemas()
            if self.deps.permission_mode_controller is not None:
                self.deps.permission_mode_controller.notify_state_changed()
            if self.deps.ui is not None:
                self.deps.ui.on_system_state_changed()

        topic = target_session.get("topic", "")
        msg_count = len(messages)
        task_info = ""
        if self._task_mgr.has_open_items():
            task_list = self._task_mgr.list_tasks()
            open_count = sum(1 for t in task_list["tasks"] if t["status"] != "completed")
            task_info = f"，{open_count} 个未完成任务"

        await self.deps.event_bus.request_output(
            f"已恢复会话 {target_id[:8]}...（{msg_count} 条消息{task_info}{mode_info}）\n"
        )

        # 向 session_context 注入恢复提示，让 LLM 知道上下文来自恢复
        self.deps.session_context.append(
            f"当前会话已从历史会话恢复（session {target_id[:8]}...）。"
            f"会话主题: \"{topic}\"。"
            "请基于恢复的上下文继续对话。"
        )

        # 清除系统提示词缓存以反映新的 session_context
        self._prompt_mgr.invalidate_cache()

    async def _handle_mode_command(self) -> None:
        """处理 /mode 命令：委托给 PermissionModeController。"""
        controller = self.deps.permission_mode_controller
        if controller is not None:
            await controller.prompt_selection(self)

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
        ctx.manual_compact = False
        ctx.compact_focus = None
        called_tools: list[str] = []

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

            called_tools.append(tool_name)
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
        for msg in self._reminder_mgr.collect_post_round_messages(self.deps.permission_mgr):
            ctx.messages.append(msg)

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
            permission_mgr = self.deps.permission_mgr
            perm_mode = permission_mgr.mode.value if permission_mgr else ""
            pre_plan = ""
            if permission_mgr and permission_mgr._pre_plan_mode is not None:
                pre_plan = permission_mgr._pre_plan_mode.value
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
