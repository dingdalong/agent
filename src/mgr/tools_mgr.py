import logging
import time
from typing import Any, Dict, TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError

from src.events.types import ToolCallCompleted, ToolCallStarted
from src.tools import ToolDict, ToolEntry

if TYPE_CHECKING:
    from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)

class ToolsMgr:
    def __init__(self, load_registered: bool = True):
        self._tools: dict[str, ToolEntry] = {}
        self._result_store: dict[str, list[str]] = {}
        if not load_registered:
            return
        from src.tools.decorator import _registry
        for entry in _registry:
            self.register(entry)

    def register(self, tool: ToolEntry) -> None:
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已注册，跳过")
            return
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_entries(self) -> list[ToolEntry]:
        return list(self._tools.values())

    def get_schemas(self, tool_names: set[str] | list[str] | None = None) -> list[ToolDict]:
        if tool_names is None:
            tools = list(self._tools.values())
        else:
            tools = [self._tools[name] for name in sorted(tool_names) if name in self._tools]
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
        """返回格式化后的分页工具结果。"""
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
        if llm.estimate_tokens([{"role": "tool", "content": result}]) <= llm.page_token_budget:
            return result
        pages = llm.split_page(result)
        self._result_store[tool_call_id] = pages
        return "工具调用结果过长，已被自动分页。可调用read_tool_result读取后续内容。\n" + self.get_page(tool_call_id, 1)

    def _tool_detail(self, tool: ToolEntry, arguments: dict[str, Any]) -> str:
        tips = tool.permission.tips if tool.permission else None
        if not tips:
            return tool.description
        try:
            values = {**arguments, **tool.model(**arguments).model_dump()}
        except ValidationError:
            values = arguments
        try:
            return tips.format(**values)
        except (AttributeError, IndexError, KeyError, ValueError):
            return tips

    def _result_status(self, result: str) -> str:
        error_prefixes = (
            "错误：",
            "参数验证失败:",
            "工具执行出错:",
            "权限拒绝：",
        )
        return "error" if result.startswith(error_prefixes) else "success"

    def _result_preview(self, result: str, limit: int = 160) -> str:
        preview = " ".join(result.split())
        if len(preview) <= limit:
            return preview
        return preview[: limit - 3] + "..."


    async def _emit_tool_started(
        self,
        deps: Any,
        context: dict[str, Any],
        tool: ToolEntry,
        arguments: dict[str, Any],
        tool_call_id: str,
    ) -> None:
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        agent = context.get("agent")
        await event_bus.emit(ToolCallStarted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=tool_call_id,
            detail=self._tool_detail(tool, arguments),
            caller_agent_type=getattr(agent, "agent_type", None),
            caller_uuid=self._format_uuid(getattr(agent, "uuid", None)),
        ))

    async def _emit_tool_completed(
        self,
        deps: Any,
        context: dict[str, Any],
        tool: ToolEntry,
        tool_call_id: str,
        status: str,
        duration_seconds: float,
        result: str,
    ) -> None:
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        agent = context.get("agent")
        await event_bus.emit(ToolCallCompleted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=tool_call_id,
            status=status,
            duration_seconds=duration_seconds,
            result_preview=self._result_preview(result),
            caller_agent_type=getattr(agent, "agent_type", None),
            caller_uuid=self._format_uuid(getattr(agent, "uuid", None)),
        ))

    def _format_uuid(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: Dict[str, Any] | None = None) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）"""
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        context = context or {}
        deps = context.get("deps")
        agent = context.get("agent")
        tool_call_id = context.get("current_tool_call_id") or ""
        hooks_mgr = getattr(deps, "hooks_mgr", None) if deps is not None else None
        agent = context.get("agent")
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
                {"tool_name": tool.name, "tool_input": arguments, "tool_use_id": tool_call_id},
                pre_tool=True,
                **hook_kwargs,
            )
            # hook blocked (exit code 2) → 直接拒绝
            if pre_hook_result.blocked:
                return f"权限拒绝：{pre_hook_result.block_reason or 'hook blocked'}"
            # hook deny → 直接拒绝，不进内置权限
            for decision, reason in pre_hook_result.permission_decisions:
                if decision == "deny":
                    return f"权限拒绝：{reason}"
            if pre_hook_result.updated_input is not None:
                arguments = pre_hook_result.updated_input

        # 2. 内置权限检查（用 hook 可能修改后的 input）
        permission_mgr = getattr(deps, "permission_mgr", None) if deps is not None else None
        if permission_mgr is not None:
            builtin_permission, builtin_reason = permission_mgr.check(tool, arguments)
            # hook ask 或内置 ask → 取最严格的
            hook_has_ask = pre_hook_result is not None and any(
                d == "ask" for d, _ in pre_hook_result.permission_decisions
            )
            if builtin_permission == "deny":
                await permission_mgr.notify_decision(tool, arguments, deps, "deny")
                return f"权限拒绝：{builtin_reason}"
            if builtin_permission == "ask" or hook_has_ask:
                permission, reason = await permission_mgr.resolve_ask(
                    tool, arguments, deps,
                    persist_allowed=builtin_permission == "ask",
                )
                if permission == "deny":
                    await permission_mgr.notify_decision(tool, arguments, deps, "deny")
                    return f"权限拒绝：{reason}"
            else:
                await permission_mgr.notify_decision(tool, arguments, deps, builtin_permission)

        await self._emit_tool_started(deps, context, tool, arguments, tool_call_id)
        started_at = time.time()
        result = await tool(context, **arguments)
        if hooks_mgr is not None:
            post_hook_result = await hooks_mgr.run_event(
                "PostToolUse",
                tool.name,
                {"tool_name": tool.name, "tool_input": arguments, "tool_response": result, "tool_use_id": tool_call_id},
                **hook_kwargs,
            )
            if post_hook_result.blocked:
                result = f"权限拒绝：{post_hook_result.block_reason or 'hook blocked'}"
            elif post_hook_result.additional_context:
                result = result + "\n\n" + "\n\n".join(str(item) for item in post_hook_result.additional_context)
        status = self._result_status(result)
        await self._emit_tool_completed(
            deps,
            context,
            tool,
            tool_call_id,
            status,
            time.time() - started_at,
            result,
        )
        if tool.raw_output:
            return result

        if not tool_call_id:
            return result
        llm = getattr(agent, "llm", None) if agent is not None else None
        if llm is None:
            return result

        return self._truncate(result, tool_call_id, llm)
