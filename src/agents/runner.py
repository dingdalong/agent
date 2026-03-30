"""AgentRunner — 驱动单个 Agent 完成任务的工具调用循环。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.agents.agent import Agent, AgentResult, HandoffRequest
from src.agents.context import RunContext, TraceEvent, AppState
from src.agents.registry import AgentRegistry
from src.guardrails import run_guardrails
from src.llm.structured import build_output_schema, parse_output

logger = logging.getLogger(__name__)

HANDOFF_PREFIX = "transfer_to_"


class AgentRunner:
    """驱动单个 Agent 完成任务的循环。

    职责边界：只负责一个 agent 的工具调用循环和 handoff 检测。
    不关心图的拓扑 — 图引擎决定收到 HandoffRequest 后怎么做。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        max_tool_rounds: int = 10,
        max_result_length: int = 4000,
    ):
        self.registry = registry
        self.max_tool_rounds = max_tool_rounds
        self.max_result_length = max_result_length

    async def run(self, agent: Agent, context: RunContext) -> AgentResult:
        """执行 agent，返回 AgentResult。"""
        hooks = agent.hooks

        # 1. hooks.on_start
        if hooks:
            await hooks.on_start(agent, context)

        # 2. input guardrails
        block = await run_guardrails(agent.input_guardrails, context, context.input)
        if block:
            return AgentResult(text=block.message)

        # 3. 构建 instructions
        if callable(agent.instructions):
            system_prompt = agent.instructions(context)
        else:
            system_prompt = agent.instructions

        # 4. 构建 messages
        task = context.input
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        # 注入长期记忆上下文和对话历史（AppState 有显式字段，其他 state 类型走 getattr）
        if isinstance(context.state, AppState):
            memory_context = context.state.memory_context
            conversation_history = context.state.conversation_history
        else:
            memory_context = getattr(context.state, "memory_context", None)
            conversation_history = getattr(context.state, "conversation_history", None)

        if memory_context:
            messages.append({
                "role": "system",
                "content": f"[相关记忆]\n{memory_context}",
            })
        if conversation_history:
            for msg in conversation_history:
                if msg.get("role") == "system":
                    continue
                messages.append(msg)
            # 避免重复添加当前用户消息
            if not conversation_history or conversation_history[-1].get("content") != task:
                messages.append({"role": "user", "content": task})
        else:
            messages.append({"role": "user", "content": task})

        # 5. 按需连接 MCP server，然后构建工具列表
        if agent.tools:
            tool_router = getattr(context.deps, "tool_router", None)
            if tool_router:
                await tool_router.ensure_tools(agent.tools)
        tools = self._build_tools(agent, context)
        handoff_tools = self._build_handoff_tools(agent)
        all_tools = tools + handoff_tools
        if not all_tools:
            all_tools = None

        # 6. 工具调用循环
        final_text = ""
        for round_idx in range(self.max_tool_rounds):
            response = await context.deps.llm.chat(
                messages,
                tools=all_tools,
                silent=True,
            )
            content, tool_calls = response.content, response.tool_calls

            if not tool_calls:
                final_text = content
                break

            # 检查是否有 handoff 调用
            for tc in tool_calls.values():
                tc_name = tc.get("name", "")
                if tc_name.startswith(HANDOFF_PREFIX):
                    target_name = tc_name[len(HANDOFF_PREFIX):]
                    try:
                        args = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    handoff = HandoffRequest(
                        target=target_name,
                        task=args.get("task", context.input),
                    )
                    context.trace.append(TraceEvent(
                        node=agent.name,
                        event="handoff",
                        timestamp=time.time(),
                        data={"target": target_name, "task": handoff.task},
                    ))
                    if hooks:
                        await hooks.on_handoff(agent, context, handoff)
                    return AgentResult(text=content or "", handoff=handoff)

            # 普通工具调用
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls.values()
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls.values():
                tool_name = tc["name"]
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}

                context.trace.append(TraceEvent(
                    node=agent.name,
                    event="tool_call",
                    timestamp=time.time(),
                    data={"tool": tool_name, "args": args},
                ))
                if hooks:
                    await hooks.on_tool_call(agent, context, tool_name, args)

                tool_router = getattr(context.deps, "tool_router", None)
                if tool_router:
                    result_text = await tool_router.route(tool_name, args)
                else:
                    result_text = "Error: no tool_router in deps"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result_text),
                })
        else:
            # 超过 max_tool_rounds
            response = await context.deps.llm.chat(messages, silent=True)
            final_text = response.content

        # 截断
        if len(final_text) > self.max_result_length:
            final_text = final_text[: self.max_result_length] + "...(已截断)"

        # 7. output guardrails
        block = await run_guardrails(agent.output_guardrails, context, final_text)
        if block:
            final_text = block.message

        # 8. 结构化输出
        structured_data: dict = {}
        if agent.output_model is not None:
            output_schema = build_output_schema(
                "agent_output",
                f"将结果整理为 {agent.output_model.__name__} 结构",
                agent.output_model,
            )
            struct_response = await context.deps.llm.chat(
                messages + [{"role": "user", "content": "请将结果整理为结构化数据。"}],
                tools=[output_schema],
                silent=True,
            )
            parsed = parse_output(struct_response.tool_calls, "agent_output", agent.output_model)
            if parsed is not None:
                structured_data = parsed.model_dump()

        result = AgentResult(text=final_text, data=structured_data)

        # 9. hooks.on_end
        if hooks:
            await hooks.on_end(agent, context, result)

        return result

    def _build_tools(self, agent: Agent, context: RunContext) -> list[dict]:
        """从 deps.tool_router 过滤 agent 允许的工具。"""
        tool_router = getattr(context.deps, "tool_router", None)
        if not tool_router or not agent.tools:
            return []
        all_schemas = tool_router.get_all_schemas()
        return [s for s in all_schemas if s["function"]["name"] in agent.tools]

    def _build_handoff_tools(self, agent: Agent) -> list[dict]:
        """为 agent.handoffs 生成 transfer_to_<name> 工具。"""
        tools = []
        for target_name in agent.handoffs:
            target = self.registry.get(target_name)
            description = target.description if target else target_name
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{HANDOFF_PREFIX}{target_name}",
                    "description": f"将任务交接给 {target_name}: {description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "交接给目标 agent 的任务描述",
                            }
                        },
                        "required": ["task"],
                    },
                },
            })
        return tools
