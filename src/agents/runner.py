"""AgentRunner — 驱动单个 Agent 完成任务的工具调用循环。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.agents.agent import Agent, AgentResult, HandoffRequest
from src.agents.context import RunContext, TraceEvent
from src.events.bus import EventBus
from src.events.types import (
    AgentStarted,
    AgentEnded,
    ToolCalled as ToolCalledEvent,
    ToolResult as ToolResultEvent,
    Handoff as HandoffEvent,
    ErrorOccurred,
)
from src.graph.messages import AgentMessage, AgentResponse
from src.guardrails import run_guardrails
from src.llm.structured import build_output_schema, parse_output

logger = logging.getLogger(__name__)

HANDOFF_PREFIX = "transfer_to_"
SYSTEM_TOOLS = {"ask_user"}


class AgentRunner:
    """驱动单个 Agent 完成任务的循环。

    职责边界：只负责一个 agent 的工具调用循环和 handoff 检测。
    不关心图的拓扑 — 图引擎决定收到 HandoffRequest 后怎么做。
    """

    def __init__(
        self,
        max_tool_rounds: int = 10,
        max_result_length: int = 4000,
        event_bus: EventBus | None = None,
    ):
        self.max_tool_rounds = max_tool_rounds
        self.max_result_length = max_result_length
        self._bus = event_bus

    async def run(self, agent: Agent, context: RunContext) -> AgentResult:
        """执行 agent，返回 AgentResult。"""
        # 1. EventBus: agent_started
        if self._bus:
            await self._bus.emit(AgentStarted(
                timestamp=time.time(), source=agent.name, agent_name=agent.name,
            ))

        # 2. input guardrails
        block = await run_guardrails(agent.input_guardrails, context, context.input)
        if block:
            return AgentResult(response=AgentResponse(text=block.message, sender=agent.name))

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

        # 注入长期记忆上下文和对话历史
        memory_context = context.get_memory_context()
        conversation_history = context.get_conversation_history()

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

        history_end_idx = len(messages)

        # 5. 按需连接 MCP server，然后构建工具列表
        tool_router = getattr(context.deps, "tool_router", None)
        if tool_router:
            if agent.tools:
                await tool_router.ensure_tools(agent.tools)
        tools = self._build_tools(agent, context)
        handoff_tools = self._build_handoff_tools(agent, context)
        all_tools = tools + handoff_tools
        if not all_tools:
            all_tools = None

        # 6. 工具调用循环
        final_text = ""
        for round_idx in range(self.max_tool_rounds):
            response = await context.deps.llm.chat(
                messages,
                tools=all_tools,
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
                    message = AgentMessage(
                        objective=args.get("objective", args.get("task", context.input)),
                        task=args.get("task", context.input),
                        context=args.get("context", ""),
                        expected_result=args.get("expected_result"),
                        sender=agent.name,
                    )
                    handoff = HandoffRequest(target=target_name, message=message)
                    context.trace.append(TraceEvent(
                        node=agent.name,
                        event="handoff",
                        timestamp=time.time(),
                        data={"target": target_name, "task": message.task},
                    ))
                    if self._bus:
                        await self._bus.emit(HandoffEvent(
                            timestamp=time.time(), source=agent.name,
                            from_agent=agent.name, to_agent=target_name, task=message.task,
                        ))
                    self._persist_turns(context, messages, history_end_idx, content or "")
                    return AgentResult(
                        response=AgentResponse(text=content or "", sender=agent.name),
                        handoff=handoff,
                    )

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
                if self._bus:
                    await self._bus.emit(ToolCalledEvent(
                        timestamp=time.time(), source=agent.name,
                        tool_name=tool_name, args=args,
                    ))

                tool_router = getattr(context.deps, "tool_router", None)
                if tool_router:
                    result_text = await tool_router.route(tool_name, args, context)
                else:
                    result_text = "Error: no tool_router in deps"

                if self._bus:
                    await self._bus.emit(ToolResultEvent(
                        timestamp=time.time(), source=agent.name,
                        tool_name=tool_name, result=str(result_text)[:500],
                    ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result_text),
                })
        else:
            # 超过 max_tool_rounds
            response = await context.deps.llm.chat(messages)
            final_text = response.content

        # 持久化对话轮次，供后续节点参考
        self._persist_turns(context, messages, history_end_idx, final_text)

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
                f"从文本中提取结构化数据，填入 {agent.output_model.__name__} 格式",
                agent.output_model,
            )
            struct_messages: list[dict[str, Any]] = [
                {"role": "system", "content": f"从以下文本中提取结构化数据，填入 {agent.output_model.__name__} 格式。"},
                {"role": "user", "content": final_text},
            ]
            struct_response = await context.deps.llm.chat(
                struct_messages,
                tools=[output_schema],
                tool_choice={"type": "function", "function": {"name": "agent_output"}},
                silent=True,
            )
            parsed = parse_output(struct_response.tool_calls, "agent_output", agent.output_model)
            if parsed is not None:
                structured_data = parsed.model_dump()

        result = AgentResult(
            response=AgentResponse(text=final_text, data=structured_data, sender=agent.name),
        )

        # 9. EventBus: agent_ended
        if self._bus:
            await self._bus.emit(AgentEnded(
                timestamp=time.time(), source=agent.name, agent_name=agent.name,
            ))

        return result

    def _persist_turns(
        self,
        context: RunContext,
        messages: list[dict[str, Any]],
        history_end_idx: int,
        final_text: str = "",
    ) -> None:
        """将本轮有意义的对话写回 conversation_history，供后续节点参考。

        只保留：用户消息、ask_user 问答、最终助手回复，
        跳过内部工具调用细节，保持历史简洁。
        """
        new_turns: list[dict[str, Any]] = []
        ask_user_ids: set[str] = set()

        for msg in messages[history_end_idx:]:
            role = msg.get("role")
            content = msg.get("content")

            if role == "user" and content:
                new_turns.append({"role": "user", "content": content})
            elif role == "assistant":
                has_ask_user = False
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    if func.get("name") == "ask_user":
                        has_ask_user = True
                        ask_user_ids.add(tc["id"])
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        question = args.get("question", "")
                        if question:
                            parts = [p for p in [content, question] if p]
                            new_turns.append({
                                "role": "assistant",
                                "content": "\n".join(parts),
                            })
                            content = None  # 避免下面重复添加
                # 非 ask_user 的纯文本回复
                if content and not has_ask_user:
                    new_turns.append({"role": "assistant", "content": content})
            elif role == "tool":
                # 只保留 ask_user 的结果（用户的实际回答）
                if msg.get("tool_call_id") in ask_user_ids and content:
                    new_turns.append({"role": "user", "content": content})

        # 追加最终回复
        if final_text and (not new_turns or new_turns[-1].get("content") != final_text):
            new_turns.append({"role": "assistant", "content": final_text})

        if not new_turns:
            return

        context.extend_history(new_turns)

    def _build_tools(self, agent: Agent, context: RunContext) -> list[dict]:
        """从 deps.tool_router 过滤 agent 允许的工具。

        - 系统工具（SYSTEM_TOOLS）始终包含，所有 agent 可用
        - 当 context.delegate_depth >= 1 时，过滤掉所有 delegate_ 前缀的工具
        """
        tool_router = getattr(context.deps, "tool_router", None)
        if not tool_router:
            return []
        all_schemas = tool_router.get_all_schemas()

        if not agent.tools:
            return [s for s in all_schemas if s["function"]["name"] in SYSTEM_TOOLS]

        allowed = set(agent.tools) | SYSTEM_TOOLS
        if context.delegate_depth >= 1:
            allowed = {
                name for name in allowed
                if not name.startswith("delegate_")
            }
        return [s for s in all_schemas if s["function"]["name"] in allowed]

    def _build_handoff_tools(self, agent: Agent, context: RunContext) -> list[dict]:
        """为 agent.handoffs 生成 transfer_to_<name> 工具，使用统一消息 schema。"""
        from src.graph.messages import build_message_schema

        tools = []
        registry = getattr(context.deps, "agent_registry", None)
        for target_name in agent.handoffs:
            target = registry.get(target_name) if registry else None
            description = target.description if target else target_name
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{HANDOFF_PREFIX}{target_name}",
                    "description": f"将任务永久交接给 {target_name}: {description}。交接后你不再处理此任务。",
                    "parameters": build_message_schema(),
                },
            })
        return tools
