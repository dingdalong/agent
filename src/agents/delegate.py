"""DelegateToolProvider — 将 Tool Agent 包装为可调用工具。

业务 Agent 可通过 delegate_<name>(objective, task, context?, expected_result?)
调用对应的 Tool Agent。委托时通过结构化的四字段 schema 强制发送方说清楚
任务意图，接收方通过 prompt 模板获得完整的任务上下文。

协议设计详见 docs/superpowers/specs/2026-03-31-structured-delegation-protocol-design.md
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.agents.context import DynamicState, RunContext
from src.agents.node import AgentNode
from src.graph.builder import GraphBuilder
from src.graph.messages import (
    AgentMessage,
    AgentResponse,
    build_message_schema,
    format_for_receiver,
)
from src.tools.schemas import ToolDict

if TYPE_CHECKING:
    from src.mcp.manager import MCPManager
    from src.tools.categories import CategoryResolver

DELEGATE_PREFIX = "delegate_"

DELEGATE_DESCRIPTION_TEMPLATE = (
    "委托任务给{description}专家。"
    "请基于当前对话上下文，清晰完整地填写以下字段，"
    "确保对方无需额外信息就能执行任务。"
)


class DelegateToolProvider:
    """将 Tool Agent 包装为可调用工具的 ToolProvider。

    实现 ToolProvider 协议（can_handle / execute / get_schemas），
    使 ToolRouter 能像路由普通工具一样路由 delegate 调用。
    """

    def __init__(
        self,
        resolver: CategoryResolver,
        mcp_manager: MCPManager | None = None,
    ) -> None:
        self._resolver = resolver
        self._mcp_manager = mcp_manager

    def can_handle(self, tool_name: str) -> bool:
        """判断 tool_name 是否为已知的 delegate 工具。"""
        if tool_name == "parallel_delegate":
            return True
        if not tool_name.startswith(DELEGATE_PREFIX):
            return False
        agent_name = tool_name[len(DELEGATE_PREFIX):]
        return self._resolver.can_resolve(agent_name)

    def get_schemas(self) -> list[ToolDict]:
        """为每个可委派的 Tool Agent 生成结构化委托 schema。"""
        schemas: list[ToolDict] = []
        for summary in self._resolver.get_all_summaries():
            name = summary["name"]
            desc = summary["description"]
            schemas.append({
                "type": "function",
                "function": {
                    "name": f"{DELEGATE_PREFIX}{name}",
                    "description": DELEGATE_DESCRIPTION_TEMPLATE.format(description=desc),
                    "parameters": build_message_schema(),
                },
            })
        # parallel_delegate 工具
        items_schema = build_message_schema()
        items_schema["properties"]["agent"] = {
            "type": "string",
            "description": "要委托的 agent 名称",
        }
        items_schema["required"] = ["agent", "objective", "task"]
        schemas.append({
            "type": "function",
            "function": {
                "name": "parallel_delegate",
                "description": "并行委托多个任务给不同 agent，同时执行并返回合并结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "description": "要并行执行的任务列表",
                            "items": items_schema,
                        },
                    },
                    "required": ["tasks"],
                },
            },
        })
        return schemas

    async def execute(self, tool_name: str, arguments: dict[str, Any], context: Any = None) -> str:
        """委派执行：构造 AgentMessage，通过 engine 或 runner 执行。

        优先通过 GraphEngine 执行（支持 handoff/tracing），
        若 engine 不可用则回退到直接 runner 调用。
        """
        if context is None:
            return "错误：delegate 调用缺少执行上下文"

        if tool_name == "parallel_delegate":
            return await self._execute_parallel(arguments, context)

        agent_name = tool_name[len(DELEGATE_PREFIX):]

        registry = getattr(context.deps, "agent_registry", None)
        if registry is None:
            return "错误：deps 中缺少 agent_registry"

        agent = registry.get(agent_name)
        if agent is None:
            return f"错误：找不到 agent {agent_name}"

        # 按需连接该 agent 所需的 MCP server
        if self._mcp_manager:
            mcp_tools = [t for t in agent.tools if t.startswith("mcp_")]
            if mcp_tools:
                await self._mcp_manager.ensure_servers_for_tools(mcp_tools)

        # 构造结构化消息
        task = arguments.get("task", "")
        message = AgentMessage(
            objective=arguments.get("objective", task),
            task=task,
            context=arguments.get("context", ""),
            expected_result=arguments.get("expected_result"),
            sender=getattr(context, "current_agent", None),
        )

        # 通过引擎执行
        engine = getattr(context.deps, "graph_engine", None)
        runner = getattr(context.deps, "runner", None)
        if engine is None or runner is None:
            # fallback：直接用 runner（兼容无 engine 的场景）
            if runner is None:
                return "错误：deps 中缺少 runner"
            receiving_input = format_for_receiver(message)
            sub_ctx = RunContext(
                input=receiving_input,
                state=DynamicState(),
                deps=context.deps,
                delegate_depth=context.delegate_depth + 1,
            )
            result = await runner.run(agent, sub_ctx)
            return result.text

        receiving_input = format_for_receiver(message)
        sub_graph = (
            GraphBuilder()
            .add_node(AgentNode(agent))
            .set_entry(agent_name)
            .compile()
        )
        sub_ctx = RunContext(
            input=receiving_input,
            state=DynamicState(),
            deps=context.deps,
            delegate_depth=context.delegate_depth + 1,
        )
        graph_result = await engine.run(sub_graph, sub_ctx)
        response = AgentResponse.from_graph_result(graph_result)
        return response.text

    async def _execute_parallel(
        self, arguments: dict[str, Any], context: Any,
    ) -> str:
        """并行执行多个 delegate 任务。"""
        import asyncio
        tasks_input = arguments.get("tasks", [])
        if not tasks_input:
            return "错误：tasks 列表为空"

        async def run_one(task_args: dict) -> str:
            agent_name = task_args.get("agent", "")
            tool_name = f"{DELEGATE_PREFIX}{agent_name}"
            return await self.execute(tool_name, task_args, context)

        results = await asyncio.gather(
            *[run_one(t) for t in tasks_input],
            return_exceptions=True,
        )

        parts = []
        for task_args, result in zip(tasks_input, results):
            agent = task_args.get("agent", "unknown")
            if isinstance(result, Exception):
                parts.append(f"[{agent}] 错误: {result}")
            else:
                parts.append(f"[{agent}] {result}")
        return "\n---\n".join(parts)
