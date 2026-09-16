"""工具管理器 — 注册、执行工具，协调权限检查和 hook。"""

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Dict, TYPE_CHECKING

from pydantic import ValidationError

from src.events.types import ToolCallCompleted, ToolCallStarted, caller_identity
from src.mode import RunMode
from src.mgr.permission_mgr import (
    ToolAuthorizationRequest,
    ToolCallerContext,
    tool_sort_order,
)
from src.tools import ToolDict, ToolEntry
from src.tools.display import ToolResult
from src.mgr.tool_output import ToolOutput
from src.tools import AccessKind, PathRole, ToolPolicy

if TYPE_CHECKING:
    from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _tool_sort_key(tool: ToolEntry) -> tuple[int, str]:
    """按声明式访问类别稳定排序。

    Args:
        tool: 工具条目。

    Returns:
        (排序权重, 工具名) 元组。
    """
    return tool_sort_order(tool.policy.access), tool.name


class ToolsMgr:
    """工具注册表与执行引擎。"""

    def __init__(self, load_registered: bool = True, output_config=None):
        self._tools: dict[str, ToolEntry] = {}
        self._schemas: list[ToolDict] | None = None
        self.output = ToolOutput(output_config)
        if not load_registered:
            return
        from src.tools.decorator import _registry
        for entry in _registry:
            self.register(entry)

    def register(self, tool: ToolEntry) -> None:
        """注册一个工具。

        Args:
            tool: 工具元数据。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具名称冲突：{tool.name}；请修改注册名称")
        if tool.origin.kind != "builtin" and tool.policy.access is not AccessKind.REVIEW:
            tool = replace(tool, policy=ToolPolicy(
                AccessKind.REVIEW,
                tool.policy.data_flow,
                tool.policy.path_args,
                False,
                tool.policy.detail_template,
            ))
        self._tools[tool.name] = tool
        self._schemas = None

    def get(self, name: str) -> ToolEntry | None:
        """按名称获取工具。"""
        return self._tools.get(name)

    def unregister_origin(self, kind: str) -> None:
        """移除指定来源的动态工具。"""
        self._tools = {
            name: entry for name, entry in self._tools.items() if entry.origin.kind != kind
        }
        self._schemas = None

    def reload(self) -> None:
        self.output.clear()

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def list_entries(self) -> list[ToolEntry]:
        """返回所有已注册的工具列表。"""
        return sorted(self._tools.values(), key=_tool_sort_key)

    def all_tool_names(self) -> set[str]:
        """返回所有已注册工具名的集合。"""
        return set(self._tools.keys())

    def schemas(self) -> list[ToolDict]:
        """返回所有已注册工具的稳定 schema 目录。"""
        if self._schemas is None:
            tools = sorted(self._tools.values(), key=_tool_sort_key)
            self._schemas = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                }
                for tool in tools
            ]
        return self._schemas

    def unavailable_in_mode(self, mode: RunMode) -> tuple[str, ...]:
        """返回当前模式不能执行的完整工具名列表。"""
        return tuple(sorted(
            entry.name for entry in self._tools.values()
            if mode not in entry.availability.modes
        ))

    def _result_preview(self, result: str, limit: int = 160) -> str:
        """生成结果预览文本。"""
        preview = " ".join(result.split())
        if len(preview) <= limit:
            return preview
        return preview[: limit - 3] + "..."

    @staticmethod
    def _external_read_preview(result: str, status: str) -> str:
        """外部读取事件只记录状态和长度，不保留网页内容。"""
        return f"status={status}, length={len(result)}"


    async def _emit_tool_started(
        self,
        deps: Any,
        agent: Any,
        tool: ToolEntry,
        safe_detail: str,
        current_tool_call_id: str,
        arguments: Dict[str, Any] | None = None,
    ) -> None:
        """发出工具调用开始事件。"""
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return

        # 生成展示数据
        display = None
        if arguments is not None:
            from src.tools.display import ToolDisplay, tool_title, format_params
            title = tool_title(tool.name)
            data_guard = getattr(deps, "data_guard", None) if deps is not None else None
            safe_args = data_guard.redact(arguments) if data_guard is not None else arguments
            params_text = format_params(tool.name, safe_args)
            display = ToolDisplay(title=title, content=params_text)

        caller_agent_type, caller_uuid = caller_identity(agent)
        await event_bus.emit(ToolCallStarted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=current_tool_call_id,
            detail=safe_detail,
            display=display,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def _emit_tool_completed(
        self,
        deps: Any,
        agent: Any,
        tool: ToolEntry,
        current_tool_call_id: str,
        status: str,
        duration_seconds: float,
        result: str,
        tool_display: object | None = None,
        original_bytes: int = 0,
        truncated: bool = False,
        error_code: str | None = None,
    ) -> None:
        """发出工具调用完成事件。"""
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        data_guard = getattr(deps, "data_guard", None) if deps is not None else None
        if tool.policy.access is AccessKind.EXTERNAL_READ:
            result_preview = self._external_read_preview(result, status)
            from src.tools.display import ToolDisplay, tool_title
            title = tool_title(tool.name)
            if status in {"error", "cancelled"}:
                title = f"✘ {title}"
            display = ToolDisplay(title=title, content=result_preview)
        else:
            safe_result = result  # 已在统一输出整理前脱敏，不再改写文件源位置表示
            result_preview = self._result_preview(str(safe_result))
            if tool_display is not None:
                # 来自 ToolResult 的展示数据，内容须经 DataGuard 脱敏
                display = tool_display
                if data_guard is not None and hasattr(display, "content") and display.content:
                    display.content = str(data_guard.redact(display.content))
            else:
                from src.tools.display import ToolDisplay, tool_title, format_result
                title = tool_title(tool.name)
                if status in {"error", "cancelled"}:
                    title = f"✘ {title}"
                content, display_truncated = format_result(str(safe_result))
                display = ToolDisplay(title=title, content=content, truncated=display_truncated)
        caller_agent_type, caller_uuid = caller_identity(agent)
        await event_bus.emit(ToolCallCompleted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=current_tool_call_id,
            status=status,
            duration_seconds=duration_seconds,
            original_bytes=original_bytes, returned_bytes=len(result.encode()),
            returned_tokens_estimate=self.output.estimate_tokens(result), truncated=truncated, error_code=error_code,
            result_preview=result_preview,
            display=display,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    @staticmethod
    def _argument_failure(tool, error):
        fields = list(tool.parameters_schema.get("properties", {}))
        details = {"allowed_fields": fields}
        if isinstance(error, ValidationError):
            details["fields"] = [list(item["loc"]) for item in error.errors()[:3]]
        return ToolResult.failure("invalid_arguments", tool.format_validation_error(error) if isinstance(error, ValidationError) else str(error),
                                  error_details=details, recovery="按当前工具 schema 修正参数；不会自动映射旧字段或执行替代命令。")

    async def _execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        current_tool_call_id: str = "",
        deps: Any = None,
        agent: Any = None,
    ) -> ToolResult:
        """执行工具调用，返回明确的结果状态。

        完整流程：参数校验 → PreToolUse hook → 再校验 → authorize → 执行 → 脱敏 → PostToolUse → 输出预算。

        Args:
            tool_name: 工具名称。
            arguments: 工具调用参数。
            current_tool_call_id: 当前工具调用的 ID。
            deps: AgentDeps 依赖对象。
            agent: 当前 Agent 实例。

        Returns:
            ToolResult，失败通过 status/error_code 表达。
        """
        if tool_name not in self._tools:
            return ToolResult.failure("unknown_tool", f"未知工具 {tool_name}")

        tool = self._tools[tool_name]
        data_guard = getattr(deps, "data_guard", None) if deps is not None else None
        if data_guard is None:
            from src.mgr.data_guard import DataGuard
            data_guard = DataGuard()

        try:
            arguments = tool.validate_arguments(arguments)
        except (ValidationError, ValueError) as error:
            return self._argument_failure(tool, error)

        hooks_mgr = getattr(deps, "hooks_mgr", None) if deps is not None else None
        hook_kwargs = {}
        if hooks_mgr is not None:
            hook_kwargs = {
                "session_id": getattr(deps, "session_id", "") if deps else "",
                "agent_id": str(getattr(agent, "uuid", "")) if agent else "",
                "agent_type": getattr(agent, "agent_type", "") if agent else "",
            }

        # 1. PreToolUse hook
        pre_hook_result = None
        if hooks_mgr is not None:
            pre_hook_result = await hooks_mgr.run_event(
                "PreToolUse",
                tool.name,
                {"tool_name": tool.name, "tool_input": arguments, "tool_use_id": current_tool_call_id},
                pre_tool=True,
                **hook_kwargs,
            )
            if pre_hook_result.blocked:
                reason = data_guard.redact(pre_hook_result.block_reason or "hook blocked")
                return ToolResult.failure("permission_denied", str(reason))
            for decision, reason in pre_hook_result.permission_decisions:
                if decision == "deny":
                    return ToolResult.failure("permission_denied", str(data_guard.redact(reason)))
            if pre_hook_result.updated_input is not None:
                try:
                    arguments = tool.validate_arguments(pre_hook_result.updated_input)
                except (ValidationError, ValueError) as error:
                    return self._argument_failure(tool, error)

        try:
            effective_budget = self.output.budget(arguments.get("max_output_tokens"))
        except ValueError as exc:
            return ToolResult.failure("invalid_arguments", str(exc))

        # 2. 唯一授权入口
        permission_mgr = getattr(deps, "permission_mgr", None) if deps is not None else None
        if permission_mgr is None:
            return ToolResult.failure("permission_denied", "授权服务不可用")

        user_intent = self._latest_user_intent(agent)
        mode = getattr(agent, "mode", RunMode.EXECUTE)
        declared_tools = getattr(agent, "tools", None)
        caller = ToolCallerContext(
            mode=mode,
            agent_type=str(getattr(agent, "agent_type", "")),
            is_subagent=bool(getattr(agent, "is_subagent", False)),
            features=frozenset(getattr(agent, "features", set()) or ()),
            declared_tools=(
                frozenset(declared_tools) if declared_tools is not None else None
            ),
            unavailable_tools=self.unavailable_in_mode(mode),
        )
        authorization = await permission_mgr.authorize(ToolAuthorizationRequest(
            tool_name=tool_name,
            policy=tool.policy,
            availability=tool.availability,
            arguments=arguments,
            origin=tool.origin,
            caller=caller,
            user_intent=user_intent,
            review_model=getattr(getattr(agent, "llm", None), "model", None),
        ))
        if not authorization.allowed:
            event_bus = getattr(deps, "event_bus", None) if deps is not None else None
            if event_bus is not None and hasattr(event_bus, "notify_permission"):
                caller_agent_type, caller_uuid = caller_identity(agent)
                await event_bus.notify_permission(
                    status="deny",
                    tool_name=tool_name,
                    detail=authorization.reason or authorization.safe_detail,
                    decision_source=authorization.source,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )
            return ToolResult.failure(authorization.error_code or "permission_denied", authorization.reason,
                                      error_details=authorization.error_details, recovery=authorization.recovery)
        elif authorization.source == "judge":
            # 智能权限放行：把放行理由提示给用户（纯展示，不影响执行）
            event_bus = getattr(deps, "event_bus", None) if deps is not None else None
            if event_bus is not None and hasattr(event_bus, "notify_permission"):
                caller_agent_type, caller_uuid = caller_identity(agent)
                await event_bus.notify_permission(
                    status="allow",
                    tool_name=tool_name,
                    detail=authorization.reason,
                    decision_source=authorization.source,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )

        await self._emit_tool_started(
            deps, agent, tool, authorization.safe_detail, current_tool_call_id,
            arguments=arguments,
        )
        context = {
            "current_tool_call_id": current_tool_call_id,
            "deps": deps,
            "agent": agent,
            "authorization": authorization,
        }
        # 叶子工具执行期间计入回合「活跃计算」，供状态栏耗时判定是否处于纯人工等待（暂停）。
        # 委派型/纯人工等待型工具（counts_as_work=False）不计，避免其嵌套的人工等待被误判为在计算。
        turn_clock = getattr(deps, "turn_clock", None) if deps is not None else None
        track_work = turn_clock is not None and tool.counts_as_work
        if track_work:
            turn_clock.enter_work()
        try:
            result = await tool(context, validated=True, **arguments)
        finally:
            if track_work:
                turn_clock.exit_work()

        # 工具写过的文件，标记共享上下文里提到它们的条目为「可能过时」。
        # 账本最高危的失败模式就是「文件已改但笔记还在描述旧代码」，而授权层已经把
        # 写路径解析好放在 authorization.grants 里，这里白拿即可。
        self._mark_context_stale(deps, authorization)

        result.text = str(data_guard.redact(result.text))
        safe_arguments = data_guard.redact(arguments)
        if hooks_mgr is not None:
            post_hook_result = await hooks_mgr.run_event(
                "PostToolUse", tool.name,
                {"tool_name": tool.name, "tool_input": safe_arguments,
                 "tool_response": str(result), "tool_use_id": current_tool_call_id},
                **hook_kwargs,
            )
            if post_hook_result.blocked:
                result = ToolResult.failure("hook_blocked", str(data_guard.redact(post_hook_result.block_reason or "hook blocked")))
            elif post_hook_result.additional_context:
                result.annotations += "\n\n".join(str(data_guard.redact(item)) for item in post_hook_result.additional_context)
        result.output_budget = effective_budget
        return result

    async def execute(self, tool_name, arguments, *, current_tool_call_id="", deps=None, agent=None):
        started = time.time()
        requested = arguments.get("max_output_tokens") if isinstance(arguments, dict) else None
        budget_error = None
        try:
            budget = self.output.budget(requested)
        except ValueError as exc:
            budget = self.output.budget(None)
            budget_error = ToolResult.failure('invalid_arguments', str(exc), recovery='省略 max_output_tokens 使用默认预算，或传入配置范围内的整数。')
        processes = getattr(deps, "process_mgr", None)
        tool = self._tools.get(tool_name)
        try:
            if budget_error is not None:
                result = budget_error
            elif processes and tool_name not in {"exec_command", "write_stdin"} and tool and tool.policy.access in {AccessKind.LOCAL_READ, AccessKind.WORKSPACE_WRITE}:
                lease = processes.workspace_lock.read() if tool.policy.access is AccessKind.LOCAL_READ else processes.workspace_lock
                async with lease:
                    result = await self._execute(tool_name, arguments, current_tool_call_id=current_tool_call_id, deps=deps, agent=agent)
            else:
                result = await self._execute(tool_name, arguments, current_tool_call_id=current_tool_call_id, deps=deps, agent=agent)
        except asyncio.CancelledError:
            result = ToolResult('工具调用已取消', status='cancelled', error_code='cancelled')
            if tool:
                await self._emit_tool_completed(deps, agent, tool, current_tool_call_id, result.status, time.time() - started, str(result))
            raise
        except Exception as exc:
            result = ToolResult.failure('tool_execution_error', str(exc))
        guard = getattr(deps, "data_guard", None)
        if guard:
            result.text = str(guard.redact(result.text))
            result.annotations = str(guard.redact(result.annotations))
            if result.error_details:
                result.error_details = guard.redact(result.error_details)
            if result.recovery:
                result.recovery = str(guard.redact(result.recovery))
        original_bytes = len(str(result).encode("utf-8"))
        control = tool_name in {"load_skill", "ask_user", "submit_plan"}
        worker = asyncio.create_task(asyncio.to_thread(self.output.finalize, result, agent, result.output_budget or budget, control=control))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # 临时日志不能在会话清理结束后又由后台线程写回来。
            await asyncio.gather(worker, return_exceptions=True)
            raise
        tool = self._tools.get(tool_name)
        if tool:
            await self._emit_tool_completed(deps, agent, tool, current_tool_call_id, result.status,
                                            time.time() - started, str(result), tool_display=result.display, original_bytes=original_bytes, truncated=result.truncated, error_code=result.error_code)
        logger.info("tool_result tool=%s call_id=%s status=%s error_code=%s original_bytes=%d returned_bytes=%d returned_tokens_estimate=%d truncated=%s elapsed=%.3f",
                    tool_name, current_tool_call_id, result.status, result.error_code, original_bytes, len(str(result).encode()),
                    self.output.estimate_tokens(str(result)), result.truncated, time.time() - started)
        return result

    @staticmethod
    def _latest_user_intent(agent: Any) -> str:
        history = getattr(agent, "history", None)
        if not isinstance(history, list):
            return ""
        for message in reversed(history):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
        return ""

    @staticmethod
    def _mark_context_stale(deps: Any, authorization: Any) -> None:
        """把共享上下文中提及本次写入文件的条目标记为可能过时。

        授权层已经把工具参数里的路径解析成 PathGrant 并标好 role，这里只挑写入类
        （WRITE / DESTINATION）回喂给 ContextMgr。纯读工具的 grants 里没有写角色，
        自然不会触发。

        Args:
            deps: 依赖容器，从中取 context_mgr。
            authorization: 本次调用的授权结果，其 grants 携带已解析的路径。

        Returns:
            None。
        """
        context_mgr = getattr(deps, "context_mgr", None) if deps is not None else None
        if context_mgr is None:
            return
        grants = getattr(authorization, "grants", None) or ()
        written = [
            grant.path
            for grant in grants
            if getattr(grant, "role", None) in (PathRole.WRITE, PathRole.DESTINATION)
        ]
        if written:
            context_mgr.mark_stale(written)
