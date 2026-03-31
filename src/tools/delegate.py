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

DELEGATE_DESCRIPTION_TEMPLATE = (
    "委托任务给{description}专家。"
    "请基于当前对话上下文，清晰完整地填写以下字段，"
    "确保对方无需额外信息就能执行任务。"
)

RECEIVING_TEMPLATE = (
    "你收到了一个委托任务：\n"
    "最终目标：{objective}\n"
    "具体任务：{task}\n"
    "{context_line}"
    "{expected_result_line}"
    "\n"
    "完成后请按以下格式返回：\n"
    "第一行标注任务状态：已完成 / 信息不足 / 失败\n"
    "之后是具体结果或需要补充的信息。\n"
    "不要猜测或假设缺失的信息。"
)


def _build_receiving_input(
    objective: str,
    task: str,
    context: str | None = None,
    expected_result: str | None = None,
) -> str:
    """用接收方模板组装委托任务的 input 文本。"""
    context_line = f"相关上下文：{context}\n" if context else ""
    expected_result_line = f"期望结果：{expected_result}\n" if expected_result else ""
    return RECEIVING_TEMPLATE.format(
        objective=objective,
        task=task,
        context_line=context_line,
        expected_result_line=expected_result_line,
    )


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
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "objective": {
                                "type": "string",
                                "description": "你的最终目标是什么（为什么需要这次委托）",
                            },
                            "task": {
                                "type": "string",
                                "description": "你需要对方具体做什么",
                            },
                            "context": {
                                "type": "string",
                                "description": "当前已知的相关信息。只填你确定知道的，不要猜测。",
                            },
                            "expected_result": {
                                "type": "string",
                                "description": "你期望对方完成后告诉你什么。如果不确定，可简要描述即可。",
                            },
                        },
                        "required": ["objective", "task"],
                    },
                },
            })
        return schemas

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """委派执行：按需连接 MCP server，创建子 RunContext 并驱动 AgentRunner。

        注意：子 RunContext 与父级共享同一 deps（含 tool_router）。
        当前 AgentRunner 串行执行工具调用，因此不存在并发问题。
        若未来支持并行工具调用，需要为每次 delegate 创建独立的 deps 副本。
        """
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
