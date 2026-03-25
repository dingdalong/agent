"""MultiAgentFlow：总控 Agent + 专业 Agent 协作的 FSM 流程。

状态流转：
  orchestrating (initial)
    ├─→ done              # LLM 直接回答（无 transfer_to_agent）
    └─→ specialist_running  # LLM 调用 transfer_to_agent

  specialist_running
    ├─→ orchestrating     # 专业 Agent 完成，结果追加为 tool 消息，返回总控
    └─→ done              # 达到最大交接次数

  done / cancelled (final)
"""

import json
import logging
from typing import Any, List, Optional

from statemachine import State, StateMachine

from config import MULTI_AGENT_MAX_HANDOFFS
from src.agents.registry import AgentRegistry
from src.agents.specialist_runner import run_specialist
from src.core.async_api import call_model
from src.core.fsm import FlowModel, OUTPUT_PREFIX
from src.core.guardrails import OutputGuardrail
from src.memory.memory import ConversationBuffer, VectorMemory
from src.tools import ToolDict
from src.tools.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

output_guard = OutputGuardrail()


class OrchestratorModel(FlowModel):
    """MultiAgentFlow 专用 model。"""

    def __init__(
        self,
        registry: AgentRegistry,
        memory: ConversationBuffer,
        user_facts: VectorMemory,
        conversation_summaries: VectorMemory,
        all_tools: List[ToolDict],
        tool_executor: ToolExecutor,
    ):
        super().__init__()
        self.registry = registry
        self.memory = memory
        self.user_facts = user_facts
        self.conversation_summaries = conversation_summaries
        self.all_tools = all_tools
        self.tool_executor = tool_executor

        # 总控对话历史（system + user + [tool call + tool result]* + assistant）
        self.messages: List[dict] = []
        # 跨 Agent 结构化状态
        self.shared_context: dict[str, dict] = {}
        self.handoff_count: int = 0
        # 待处理的交接请求 {agent_name, task, tool_call_id}
        self.pending_handoff: Optional[dict] = None
        # 是否首次进入 orchestrating
        self._initialized: bool = False


class MultiAgentFlow(StateMachine):
    """总控 + 专业 Agent 协作流程。"""

    # === 状态 ===
    orchestrating = State(initial=True)
    specialist_running = State()
    done = State(final=True)
    cancelled = State(final=True)

    # === 转移 ===
    proceed = (
        orchestrating.to(done, cond="no_handoff")
        | orchestrating.to(specialist_running, cond="has_handoff")
        | specialist_running.to(orchestrating, cond="can_continue")
        | specialist_running.to(done, cond="max_handoffs_reached")
    )
    cancel = orchestrating.to(cancelled)

    def __init__(
        self,
        registry: AgentRegistry,
        memory: ConversationBuffer,
        user_facts: VectorMemory,
        conversation_summaries: VectorMemory,
        all_tools: List[ToolDict],
        tool_executor: ToolExecutor,
    ):
        model = OrchestratorModel(
            registry=registry,
            memory=memory,
            user_facts=user_facts,
            conversation_summaries=conversation_summaries,
            all_tools=all_tools,
            tool_executor=tool_executor,
        )
        super().__init__(model=model)

    # === 条件方法 ===

    def no_handoff(self) -> bool:
        return not self.model.data.get("has_handoff", False)

    def has_handoff(self) -> bool:
        return bool(self.model.data.get("has_handoff", False))

    def can_continue(self) -> bool:
        return self.model.handoff_count < MULTI_AGENT_MAX_HANDOFFS

    def max_handoffs_reached(self) -> bool:
        return self.model.handoff_count >= MULTI_AGENT_MAX_HANDOFFS

    # === 状态回调 ===

    async def on_enter_orchestrating(self):
        """初始化或返回总控：检索记忆 → 调用 LLM → 判断是否交接。"""
        model = self.model
        user_input = model.data.get("user_input", "")

        # 首次进入：初始化消息历史
        if not model._initialized:
            model._initialized = True

            # 检索长期记忆
            memory_sections = []
            facts = [
                item.get("fact") or item.get_content()
                for item in model.user_facts.search(user_input, n_results=5)
                if isinstance(item, dict) and item.get("fact")
                   or hasattr(item, "get_content")
            ]
            facts = [f for f in facts if f]
            if facts:
                memory_sections.append("以下是你知道的关于用户的信息：\n" + "\n".join(facts))

            summaries = [
                item.get("fact") or (item.get_content() if hasattr(item, "get_content") else "")
                for item in model.conversation_summaries.search(user_input, n_results=3)
                if isinstance(item, dict) and item.get("fact")
                   or hasattr(item, "get_content")
            ]
            summaries = [s for s in summaries if s]
            if summaries:
                memory_sections.append("相关历史摘要：\n" + "\n".join(summaries))

            # 构建系统提示
            system_prompt = model.registry.build_orchestrator_system_prompt()
            if memory_sections:
                system_prompt += "\n\n" + "\n\n".join(memory_sections)

            # 追加 Skill catalog
            skill_manager = getattr(model.tool_executor, "skill_manager", None)
            if skill_manager:
                catalog = skill_manager.get_catalog_prompt()
                if catalog:
                    system_prompt += "\n\n" + catalog

            # 注入斜杠命令预激活的 skill 内容
            skill_content = model.data.get("skill_content")
            if skill_content:
                system_prompt += "\n\n" + skill_content

            model.messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ]
            model.memory.add_user_message(user_input)
            model.output_text = OUTPUT_PREFIX

        # 构建总控 LLM 工具列表
        transfer_schema = model.registry.build_transfer_tool_schema()
        orchestrator_tools = [transfer_schema]

        # 追加 activate_skill 工具
        skill_manager = getattr(model.tool_executor, "skill_manager", None)
        if skill_manager:
            activate_schema = skill_manager.build_activate_tool_schema()
            if activate_schema:
                orchestrator_tools.append(activate_schema)

        content, tool_calls, _ = await call_model(
            model.messages,
            tools=orchestrator_tools,
        )

        if tool_calls:
            # 检查是否有 activate_skill 调用
            for tc in tool_calls.values():
                if tc.get("name") == "activate_skill":
                    try:
                        args = json.loads(tc["arguments"])
                        skill_result = skill_manager.activate(args.get("name", "")) if skill_manager else None
                    except (json.JSONDecodeError, KeyError):
                        skill_result = None

                    model.messages.append({
                        "role": "assistant",
                        "content": content if content else None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["arguments"]},
                            }
                        ],
                    })
                    model.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": skill_result or "Skill not found.",
                    })
                    # 递归调用重新查询 LLM（skill 内容已在 messages 中）
                    depth = model.data.get("_skill_activation_depth", 0)
                    if depth < 3:
                        model.data["_skill_activation_depth"] = depth + 1
                        await self.on_enter_orchestrating()
                    else:
                        logger.warning("Skill 激活深度超过限制，停止递归")
                    return

            # 找到 transfer_to_agent 调用
            handoff = None
            for tc in tool_calls.values():
                if tc.get("name") == "transfer_to_agent":
                    try:
                        args = json.loads(tc["arguments"])
                        handoff = {
                            "agent_name": args.get("agent_name", ""),
                            "task": args.get("task", ""),
                            "tool_call_id": tc["id"],
                        }
                    except (json.JSONDecodeError, KeyError):
                        pass
                    break

            if handoff and model.registry.get(handoff["agent_name"]):
                # 将 assistant 的工具调用追加到消息历史
                model.messages.append({
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
                })
                model.pending_handoff = handoff
                model.data["has_handoff"] = True
                model.needs_input = False
                return

        # 无工具调用（或未知 agent）→ 直接回复
        model.memory.add_assistant_message({"role": "assistant", "content": content})
        model.data["final_response"] = content
        model.data["has_handoff"] = False
        model.data["no_handoff"] = True
        model.needs_input = False

    async def on_enter_specialist_running(self):
        """运行专业 Agent，将结果追加到总控消息历史。"""
        model = self.model
        handoff = model.pending_handoff
        if not handoff:
            model.needs_input = False
            return

        agent_def = model.registry.get(handoff["agent_name"])
        if not agent_def:
            error_text = f"未知的专业 Agent: {handoff['agent_name']}"
            logger.error(error_text)
            model.messages.append({
                "role": "tool",
                "tool_call_id": handoff["tool_call_id"],
                "content": error_text,
            })
            model.pending_handoff = None
            model.handoff_count += 1
            model.needs_input = False
            return

        # 运行专业 Agent
        result = await run_specialist(
            agent_def=agent_def,
            task=handoff["task"],
            all_tools=model.all_tools,
            tool_executor=model.tool_executor,
            shared_context=model.shared_context,
        )

        # 累积结构化状态
        if result.data:
            model.shared_context[handoff["agent_name"]] = result.data

        # 将结果作为 tool 消息追加到总控消息历史
        model.messages.append({
            "role": "tool",
            "tool_call_id": handoff["tool_call_id"],
            "content": result.text,
        })

        model.pending_handoff = None
        model.handoff_count += 1
        model.data["has_handoff"] = False  # 重置，下次 orchestrating 重新判断
        model.needs_input = False

    async def on_enter_done(self):
        """保存记忆，检查压缩，设置最终结果。"""
        model = self.model
        final_response = model.data.get("final_response", "")

        # 护栏检查
        passed, _ = output_guard.check(final_response)
        if not passed:
            final_response = "抱歉，生成的回复包含不安全内容，已过滤。"
            model.output_text = f"\n{OUTPUT_PREFIX}{final_response}\n"

        model.result = final_response

        # 存储事实到长期记忆
        user_input = model.data.get("user_input", "")
        await model.user_facts.add_conversation(user_input)

        # 检查是否需要压缩
        if model.memory.should_compress():
            await model.memory.compress(model.conversation_summaries)

    async def on_enter_cancelled(self):
        """取消流程。"""
        self.model.output_text = f"\n{OUTPUT_PREFIX}已取消。\n"
        self.model.result = "已取消。"
