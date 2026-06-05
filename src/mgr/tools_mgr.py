"""工具管理器 — 注册、执行工具，协调权限检查和 hook。"""

import logging
import time
from typing import Any, Dict, TYPE_CHECKING

from src.events.types import ToolCallCompleted, ToolCallStarted
from src.mgr.permission_mgr import tool_sort_order
from src.tools import ToolDict, ToolEntry
from src.tools.decorator import format_tool_tips

if TYPE_CHECKING:
    from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _tool_sort_key(tool: ToolEntry) -> tuple[int, str]:
    """工具排序键：只读工具优先，非只读次之，无权限元数据的排最后。

    Args:
        tool: 工具条目。

    Returns:
        (排序权重, 工具名) 元组。
    """
    if tool.permission is None:
        return tool_sort_order(None, has_permission=False), tool.name
    return tool_sort_order(tool.permission.kind), tool.name


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
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolEntry | None:
        """按名称获取工具。"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def list_entries(self) -> list[ToolEntry]:
        """返回所有已注册的工具列表。"""
        return sorted(self._tools.values(), key=_tool_sort_key)

    def get_schemas(
        self,
        tool_names: set[str] | list[str] | None = None,
        permission_mgr: Any = None,
    ) -> list[ToolDict]:
        """返回 OpenAI function-calling 格式的工具 schema 列表。

        Args:
            tool_names: 要返回的工具名集合，None 返回全部。
            permission_mgr: 权限管理器；提供时按当前模式过滤工具。

        Returns:
            工具 schema 列表。
        """
        if tool_names is None:
            tools = list(self._tools.values())
        else:
            tools = [self._tools[name] for name in tool_names if name in self._tools]
        if permission_mgr is not None:
            tools = [tool for tool in tools if permission_mgr.is_tool_visible(tool)]
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


    async def _emit_tool_started(
        self,
        deps: Any,
        agent: Any,
        tool: ToolEntry,
        arguments: dict[str, Any],
        current_tool_call_id: str,
    ) -> None:
        """发出工具调用开始事件。"""
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        await event_bus.emit(ToolCallStarted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=current_tool_call_id,
            detail=format_tool_tips(tool.permission.tips if tool.permission else None, arguments, tool.description),
            caller_agent_type=getattr(agent, "agent_type", None),
            caller_uuid=str(agent.uuid) if agent is not None and hasattr(agent, "uuid") else None,
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
    ) -> None:
        """发出工具调用完成事件。"""
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None or not hasattr(event_bus, "emit"):
            return
        await event_bus.emit(ToolCallCompleted(
            timestamp=time.time(),
            source="tools",
            tool_name=tool.name,
            tool_call_id=current_tool_call_id,
            status=status,
            duration_seconds=duration_seconds,
            result_preview=self._result_preview(result),
            caller_agent_type=getattr(agent, "agent_type", None),
            caller_uuid=str(agent.uuid) if agent is not None and hasattr(agent, "uuid") else None,
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

        完整流程：PreToolUse hook → 权限检查 → 执行 → PostToolUse hook → 分页。

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
                return f"权限拒绝：{pre_hook_result.block_reason or 'hook blocked'}"
            for decision, reason in pre_hook_result.permission_decisions:
                if decision == "deny":
                    return f"权限拒绝：{reason}"
            if pre_hook_result.updated_input is not None:
                arguments = pre_hook_result.updated_input

        # 2. 权限检查
        permission_mgr = getattr(deps, "permission_mgr", None) if deps is not None else None
        if permission_mgr is not None:
            decision, reason = permission_mgr.check(tool_name, arguments)

            hook_has_ask = pre_hook_result is not None and any(
                d == "ask" for d, _ in pre_hook_result.permission_decisions
            )

            if decision == "deny":
                await permission_mgr.notify_decision(tool_name, arguments, deps, "deny")
                return f"权限拒绝：{reason}"

            if decision == "ask" or hook_has_ask:
                resolved_decision, resolved_reason = await permission_mgr.resolve_ask(
                    tool_name, arguments, deps,
                )
                if resolved_decision == "deny":
                    await permission_mgr.notify_decision(tool_name, arguments, deps, "deny")
                    return f"权限拒绝：{resolved_reason}"
            else:
                # allow 或 auto_allow
                await permission_mgr.notify_decision(tool_name, arguments, deps, decision)

        await self._emit_tool_started(deps, agent, tool, arguments, current_tool_call_id)
        started_at = time.time()
        context = {"current_tool_call_id": current_tool_call_id, "deps": deps, "agent": agent}
        result = await tool(context, **arguments)
        if hooks_mgr is not None:
            post_hook_result = await hooks_mgr.run_event(
                "PostToolUse",
                tool.name,
                {"tool_name": tool.name, "tool_input": arguments, "tool_response": result, "tool_use_id": current_tool_call_id},
                **hook_kwargs,
            )
            if post_hook_result.blocked:
                result = f"权限拒绝：{post_hook_result.block_reason or 'hook blocked'}"
            elif post_hook_result.additional_context:
                result = result + "\n\n" + "\n\n".join(str(item) for item in post_hook_result.additional_context)
        status = self._result_status(result)
        await self._emit_tool_completed(
            deps,
            agent,
            tool,
            current_tool_call_id,
            status,
            time.time() - started_at,
            result,
        )
        if tool.raw_output:
            return result

        if not current_tool_call_id:
            return result
        llm = getattr(agent, "llm", None) if agent is not None else None
        if llm is None:
            return result

        return self._truncate(result, current_tool_call_id, llm)
