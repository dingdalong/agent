"""DelegateToolProvider — 将 Tool Agent 包装为可调用工具。

业务 Agent 可通过 delegate_tool_<name>(task="...") 调用对应的
Tool Agent，内部创建子 RunContext 并驱动 AgentRunner 执行。

本模块位于 Layer 1（src/tools/），对 Layer 2 的依赖
（AgentRunner、AgentRegistry、AgentDeps）仅在 TYPE_CHECKING
或 execute() 运行时才导入，不违反分层约束。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.tools.schemas import ToolDict

if TYPE_CHECKING:
    from src.agents.deps import AgentDeps
    from src.agents.registry import AgentRegistry
    from src.agents.runner import AgentRunner
    from src.mcp.manager import MCPManager
    from src.tools.categories import CategoryResolver

DELEGATE_PREFIX = "delegate_"


class DelegateToolProvider:
    """将 Tool Agent 包装为可调用工具的 ToolProvider。

    实现 ToolProvider 协议（can_handle / execute / get_schemas），
    使 ToolRouter 能像路由普通工具一样路由 delegate 调用。
    """

    def __init__(
        self,
        resolver: CategoryResolver,
        runner: AgentRunner,
        registry: AgentRegistry,
        deps: AgentDeps,
        mcp_manager: MCPManager | None = None,
    ) -> None:
        self._resolver = resolver
        self._runner = runner
        self._registry = registry
        self._deps = deps
        self._mcp_manager = mcp_manager
        self._delegate_depth: int = 0

    def set_delegate_depth(self, depth: int) -> None:
        """设置当前委派深度，由 ToolRouter 在每次 run 前同步。"""
        self._delegate_depth = depth

    def can_handle(self, tool_name: str) -> bool:
        """判断 tool_name 是否为已知的 delegate 工具。"""
        if not tool_name.startswith(DELEGATE_PREFIX):
            return False
        agent_name = tool_name[len(DELEGATE_PREFIX):]
        return self._resolver.can_resolve(agent_name)

    def get_schemas(self) -> list[ToolDict]:
        """为每个可委派的 Tool Agent 生成 function-calling schema。"""
        schemas: list[ToolDict] = []
        for summary in self._resolver.get_all_summaries():
            name = summary["name"]
            desc = summary["description"]
            schemas.append({
                "type": "function",
                "function": {
                    "name": f"{DELEGATE_PREFIX}{name}",
                    "description": f"委派任务给{desc}专家",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "需要完成的具体任务描述",
                            },
                        },
                        "required": ["task"],
                    },
                },
            })
        return schemas

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """委派执行：按需连接 MCP server，创建子 RunContext 并驱动 AgentRunner。"""
        from src.agents.context import DynamicState, RunContext

        agent_name = tool_name[len(DELEGATE_PREFIX):]
        agent = self._registry.get(agent_name)
        if agent is None:
            return f"错误：找不到 agent {agent_name}"

        # 按需连接该 agent 所需的 MCP server
        if self._mcp_manager:
            mcp_tools = [t for t in agent.tools if t.startswith("mcp_")]
            if mcp_tools:
                await self._mcp_manager.ensure_servers_for_tools(mcp_tools)

        sub_ctx: RunContext = RunContext(
            input=arguments.get("task", ""),
            state=DynamicState(),
            deps=self._deps,
            delegate_depth=self._delegate_depth + 1,
        )
        result = await self._runner.run(agent, sub_ctx)
        return result.text
