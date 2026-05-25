import logging
import time
from typing import Any, Dict
from uuid import UUID

from pydantic import ValidationError

from src.events.types import ToolCallCompleted, ToolCallStarted
from src.tools import ToolDict, ToolEntry

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

    def _truncate(self, result: str, tool_call_id: str, deps) -> str:
        if deps.llm.estimate_tokens([{"role": "tool", "content": result}]) <= deps.llm.page_token_budget:
            return result
        pages = deps.llm.split_page(result)
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
        if event_bus is None:
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
        if event_bus is None:
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
        if isinstance(value, UUID):
            return str(value)
        return str(value)

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: Dict[str, Any] | None = None) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）"""
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        context = context or {}
        deps = context.get("deps")
        permission_mgr = getattr(deps, "permission_mgr", None) if deps is not None else None
        if permission_mgr is not None:
            permission, reason = await permission_mgr.authorize(tool, arguments, deps)
            if permission == "deny":
                return f"权限拒绝：{reason}"

        tool_call_id = context.get("current_tool_call_id") or ""
        await self._emit_tool_started(deps, context, tool, arguments, tool_call_id)
        started_at = time.time()
        result = await tool(context, **arguments)
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
        if deps is None:
            return result

        return self._truncate(result, tool_call_id, deps)
