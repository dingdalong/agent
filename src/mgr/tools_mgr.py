"""工具管理器 — 注册、执行工具，协调权限检查和 hook。"""

import logging
import time
from dataclasses import replace
from typing import Any, Dict, TYPE_CHECKING

from pydantic import ValidationError

from src.events.types import ToolCallCompleted, ToolCallStarted, caller_identity
from src.mgr.permission_mgr import tool_sort_order
from src.tools import ToolDict, ToolEntry
from src.tools import AccessKind, ToolPolicy

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

    def __init__(self, load_registered: bool = True):
        self._tools: dict[str, ToolEntry] = {}
        self._result_store: dict[str, list[str]] = {}
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
            logger.warning(f"工具 '{tool.name}' 已注册，跳过")
            return
        if tool.origin.kind != "builtin" and tool.policy.access is not AccessKind.REVIEW:
            tool = replace(tool, policy=ToolPolicy(
                AccessKind.REVIEW,
                tool.policy.data_flow,
                tool.policy.path_args,
                False,
                tool.policy.detail_template,
            ))
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolEntry | None:
        """按名称获取工具。"""
        return self._tools.get(name)

    def unregister_origin(self, kind: str) -> None:
        """移除指定来源的动态工具。"""
        self._tools = {
            name: entry for name, entry in self._tools.items() if entry.origin.kind != kind
        }

    def reload(self) -> None:
        self._result_store.clear()

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def list_entries(self) -> list[ToolEntry]:
        """返回所有已注册的工具列表。"""
        return sorted(self._tools.values(), key=_tool_sort_key)

    def all_tool_names(self) -> set[str]:
        """返回所有已注册工具名的集合。"""
        return set(self._tools.keys())

    def excluded_tool_names(self, enabled: set[str]) -> set[str]:
        """返回因所属 feature 未启用而应被排除的工具名集合。

        扫描工具注册表，任何声明了 feature 且该 feature 不在 enabled 中的工具均被排除；
        无 feature 归属的工具恒不排除。

        Args:
            enabled: 当前 agent 启用的 feature 名集合。

        Returns:
            应排除的工具名集合。
        """
        return {e.name for e in self._tools.values() if e.feature and e.feature not in enabled}

    def resolve_subagent_tools(self, tool_names: set[str] | None) -> set[str]:
        """解析子 agent 的最终工具集。

        在 agent 定义的 tools 基础上：
        - 追加所有 subagent=True 的工具（自动注入）
        - 移除所有 subagent=False 的工具（强制排除）

        Args:
            tool_names: agent 定义中声明的工具名集合，None 表示全量。

        Returns:
            解析后的工具名集合（始终为 set，不再是 None）。
        """
        if tool_names is None:
            base = set(self._tools.keys())
        else:
            base = set(tool_names)
        for name, entry in self._tools.items():
            if entry.subagent is True:
                base.add(name)
            elif entry.subagent is False:
                base.discard(name)
        return base

    def get_schemas(
        self,
        tool_names: set[str] | list[str] | None = None,
    ) -> list[ToolDict]:
        """返回 OpenAI function-calling 格式的工具 schema 列表。

        Args:
            tool_names: 要返回的工具名集合，None 返回全部。

        Returns:
            工具 schema 列表。
        """
        if tool_names is None:
            tools = list(self._tools.values())
        else:
            tools = [self._tools[name] for name in tool_names if name in self._tools]
        tools = sorted(tools, key=_tool_sort_key)
        return [
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

    def get_page(self, tool_call_id: str, page: int) -> str:
        """返回格式化后的分页工具结果。

        Args:
            tool_call_id: 工具调用 ID。
            page: 页码（从 1 开始）。

        Returns:
            格式化的分页内容。
        """
        pages = self._result_store.get(tool_call_id)
        if pages is None:
            raise KeyError(f"未找到 tool_call_id={tool_call_id} 的缓存结果")
        total_pages = len(pages)
        if page < 1 or page > total_pages:
            raise ValueError(f"页码超出范围：page={page}，总页数为 {total_pages}")
        content = pages[page - 1]
        parts = [
            f"tool_call_id: {tool_call_id} | 总页数: {total_pages} | 当前第 {page} 页：",
            content,
        ]
        if page < total_pages:
            parts.append(f"传入 tool_call_id={tool_call_id}, page={page + 1} 继续读取")
        return "\n".join(parts)

    def _truncate(self, result: str, tool_call_id: str, llm: LLMProvider) -> str:
        """超长结果自动分页。"""
        if llm.estimate_tokens([{"role": "tool", "content": result}]) <= llm.page_token_budget:
            return result
        pages = llm.split_page(result)
        self._result_store[tool_call_id] = pages
        return "工具调用结果过长，已被自动分页。可调用read_tool_result读取后续内容。\n" + self.get_page(tool_call_id, 1)

    def _result_status(self, result: str) -> str:
        """根据结果内容判断状态。"""
        error_prefixes = (
            "错误：",
            "参数验证失败:",
            "工具执行出错:",
            "权限拒绝：",
        )
        return "error" if result.startswith(error_prefixes) else "success"

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
            if tool.policy.access is AccessKind.EXTERNAL_READ:
                params_text = ""
            else:
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
    ) -> None:
        """发出工具调用完成事件。"""
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        data_guard = getattr(deps, "data_guard", None) if deps is not None else None
        if tool.policy.access is AccessKind.EXTERNAL_READ:
            result_preview = self._external_read_preview(result, status)
            display = None  # EXTERNAL_READ 不展示详情
        else:
            safe_result = data_guard.redact(result) if data_guard is not None else result
            result_preview = self._result_preview(str(safe_result))
            if tool_display is not None:
                # 来自 ToolResult 的展示数据，内容须经 DataGuard 脱敏
                display = tool_display
                if data_guard is not None and hasattr(display, "content") and display.content:
                    display.content = str(data_guard.redact(display.content))
            else:
                from src.tools.display import ToolDisplay, tool_title, format_result
                title = tool_title(tool.name)
                if status != "success":
                    title = f"✘ {title}"
                content, truncated = format_result(str(safe_result))
                display = ToolDisplay(title=title, content=content, truncated=truncated)
        caller_agent_type, caller_uuid = caller_identity(agent)
        await event_bus.emit(ToolCallCompleted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=current_tool_call_id,
            status=status,
            duration_seconds=duration_seconds,
            result_preview=result_preview,
            display=display,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        current_tool_call_id: str = "",
        deps: Any = None,
        agent: Any = None,
    ) -> str:
        """执行工具调用，返回结果字符串。

        完整流程：参数校验 → PreToolUse hook → 再校验 → authorize → 执行 → 脱敏 → PostToolUse → 事件/分页。

        Args:
            tool_name: 工具名称。
            arguments: 工具调用参数。
            current_tool_call_id: 当前工具调用的 ID。
            deps: AgentDeps 依赖对象。
            agent: 当前 Agent 实例。

        Returns:
            工具执行结果字符串（错误信息也以字符串返回）。
        """
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        data_guard = getattr(deps, "data_guard", None) if deps is not None else None
        if data_guard is None:
            from src.mgr.data_guard import DataGuard
            data_guard = DataGuard()

        try:
            arguments = tool.validate_arguments(arguments)
        except ValidationError as error:
            return tool.format_validation_error(error)

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
                return f"权限拒绝：{reason}"
            for decision, reason in pre_hook_result.permission_decisions:
                if decision == "deny":
                    return f"权限拒绝：{data_guard.redact(reason)}"
            if pre_hook_result.updated_input is not None:
                try:
                    arguments = tool.validate_arguments(pre_hook_result.updated_input)
                except ValidationError as error:
                    return tool.format_validation_error(error)

        # 2. 唯一授权入口
        permission_mgr = getattr(deps, "permission_mgr", None) if deps is not None else None
        if permission_mgr is None:
            return "权限拒绝：授权服务不可用"

        user_intent = self._latest_user_intent(agent)
        authorization = await permission_mgr.authorize(
            tool_name,
            tool.policy,
            arguments,
            origin=tool.origin,
            plan_active=bool(getattr(agent, "plan_active", False)),
            user_intent=user_intent,
            review_model=getattr(getattr(agent, "llm", None), "model", None),
        )
        if not authorization.allowed:
            event_bus = getattr(deps, "event_bus", None) if deps is not None else None
            if event_bus is not None and hasattr(event_bus, "notify_permission"):
                caller_agent_type, caller_uuid = caller_identity(agent)
                await event_bus.notify_permission(
                    status="deny",
                    tool_name=tool_name,
                    detail=authorization.reason or authorization.safe_detail,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )
            return f"权限拒绝：{authorization.reason}"
        elif authorization.source == "judge":
            # 智能权限放行：把放行理由提示给用户（纯展示，不影响执行）
            event_bus = getattr(deps, "event_bus", None) if deps is not None else None
            if event_bus is not None and hasattr(event_bus, "notify_permission"):
                caller_agent_type, caller_uuid = caller_identity(agent)
                await event_bus.notify_permission(
                    status="allow",
                    tool_name=tool_name,
                    detail=authorization.reason,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )

        await self._emit_tool_started(
            deps, agent, tool, authorization.safe_detail, current_tool_call_id,
            arguments=arguments,
        )
        started_at = time.time()
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

        # 提取 ToolResult：工具可返回 ToolResult 携带展示侧数据
        tool_display_result = None
        from src.tools.display import ToolResult as _ToolResult
        if isinstance(result, _ToolResult):
            tool_display_result = result.display
            result = result.text

        result = self._limit_result(str(data_guard.redact(result)))
        safe_arguments = data_guard.redact(arguments)
        if hooks_mgr is not None:
            post_hook_result = await hooks_mgr.run_event(
                "PostToolUse",
                tool.name,
                {"tool_name": tool.name, "tool_input": safe_arguments, "tool_response": result, "tool_use_id": current_tool_call_id},
                **hook_kwargs,
            )
            if post_hook_result.blocked:
                result = f"权限拒绝：{post_hook_result.block_reason or 'hook blocked'}"
            elif post_hook_result.additional_context:
                result = result + "\n\n" + "\n\n".join(
                    str(data_guard.redact(item)) for item in post_hook_result.additional_context
                )
        result = self._limit_result(str(data_guard.redact(result)))
        status = self._result_status(result)
        await self._emit_tool_completed(
            deps,
            agent,
            tool,
            current_tool_call_id,
            status,
            time.time() - started_at,
            result,
            tool_display=tool_display_result,
        )
        if tool.raw_output:
            return result

        if not current_tool_call_id:
            return result
        llm = getattr(agent, "llm", None) if agent is not None else None
        if llm is None:
            return result

        return self._truncate(result, current_tool_call_id, llm)

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
    def _limit_result(result: str) -> str:
        lines = result.splitlines(keepends=True)
        if len(lines) > 20_000:
            result = "".join(lines[:20_000]) + "\n[结果已截断]"
        encoded = result.encode()
        if len(encoded) > 1024 * 1024:
            result = encoded[:1024 * 1024].decode(errors="replace") + "\n[结果已截断]"
        return result
