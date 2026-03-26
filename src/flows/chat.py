"""ChatFlow：普通对话流程，替代 main.py 中的 run_agent 逻辑。

状态流转：retrieving → calling ⇄ tool_executing → done
"""

import logging
from typing import Any, Dict, List, Optional

from statemachine import StateMachine, State

from src.core.async_api import call_model
from src.core.fsm import FlowModel, OUTPUT_PREFIX
from src.core.guardrails import OutputGuardrail
from src.core.performance import async_time_function
from src.memory import ConversationBuffer, MemoryStore, MemoryType
from src.tools import ToolDict
from src.tools.tool_call import execute_tool_calls
from src.tools.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5

output_guard = OutputGuardrail()


class ChatModel(FlowModel):
    """ChatFlow 专用 model，携带对话所需的所有引用。"""

    def __init__(
        self,
        memory: ConversationBuffer,
        store: MemoryStore,
        tools_schema: List[ToolDict],
        tool_executor: ToolExecutor,
    ):
        super().__init__()
        self.memory = memory
        self.store = store
        self.tools_schema = tools_schema
        self.tool_executor = tool_executor
        self.tool_call_count = 0


class ChatFlow(StateMachine):
    """普通对话：记忆检索 → 模型调用 → [工具循环] → 完成"""

    # 状态
    retrieving = State(initial=True)
    calling = State()
    tool_executing = State()
    done = State(final=True)

    # 转移 - 全部通过 proceed 事件驱动
    proceed = (
        retrieving.to(calling)
        | calling.to(done, cond="no_tool_calls")
        | calling.to(tool_executing, cond="has_tool_calls")
        | tool_executing.to(calling, cond="can_continue_tools")
        | tool_executing.to(done, cond="max_tools_reached")
    )

    def __init__(self, **kwargs):
        # kwargs 中的参数传给 ChatModel
        model = ChatModel(**kwargs)
        super().__init__(model=model)

    # === 条件 ===

    def no_tool_calls(self) -> bool:
        return not self.model.data.get("pending_tool_calls")

    def has_tool_calls(self) -> bool:
        return bool(self.model.data.get("pending_tool_calls"))

    def can_continue_tools(self) -> bool:
        return self.model.tool_call_count < MAX_TOOL_CALLS

    def max_tools_reached(self) -> bool:
        return self.model.tool_call_count >= MAX_TOOL_CALLS

    # === 状态回调 ===

    async def on_enter_retrieving(self):
        """检索长期记忆，构建增强 system prompt，将用户消息加入短期记忆。"""
        user_input = self.model.data["user_input"]
        memory = self.model.memory
        store = self.model.store

        # 检索记忆
        memory_sections = []
        facts = store.search(user_input, n=10, memory_type=MemoryType.FACT)
        if facts:
            memory_sections.append(
                "以下是你知道的关于用户的信息：\n" + "\n".join(r.content for r in facts)
            )

        summaries = store.search(user_input, n=5, memory_type=MemoryType.SUMMARY)
        if summaries:
            memory_sections.append(
                "以下是与当前对话相关的历史摘要：\n" + "\n".join(r.content for r in summaries)
            )

        enhanced_system = "你是一个很棒的助手！"
        if memory_sections:
            enhanced_system += "\n\n" + "\n\n".join(memory_sections)
        enhanced_system += "\n\n如果工具返回错误，请分析错误信息并尝试重新调用（调整参数），或向用户解释。"

        self.model.data["system_prompt"] = enhanced_system

        # 将用户消息加入短期记忆
        memory.add_user_message(user_input)

        # 输出前缀（call_model 的流式输出不带前缀）
        self.model.output_text = OUTPUT_PREFIX

        # 不需要用户输入，自动转移
        self.model.needs_input = False

    async def on_enter_calling(self):
        """调用模型，判断是否有工具调用。"""
        memory = self.model.memory
        system_prompt = self.model.data["system_prompt"]

        messages = [{"role": "system", "content": system_prompt}] + memory.get_messages_for_api()
        content, tool_calls, _ = await call_model(
            messages, tools=self.model.tools_schema
        )

        if tool_calls:
            self.model.data["pending_tool_calls"] = tool_calls
            self.model.data["pending_content"] = content
        else:
            # 没有工具调用，保存回复到短期记忆
            memory.add_assistant_message({"role": "assistant", "content": content})
            self.model.data["final_response"] = content
            self.model.data["pending_tool_calls"] = None

        self.model.needs_input = False

    async def on_enter_tool_executing(self):
        """执行工具调用，结果存入短期记忆。"""
        memory = self.model.memory
        tool_calls = self.model.data["pending_tool_calls"]
        content = self.model.data.get("pending_content", "")

        new_messages = await execute_tool_calls(content, tool_calls, self.model.tool_executor)
        memory.add_assistant_message(new_messages[0])
        for tool_msg in new_messages[1:]:
            memory.add_tool_message(tool_msg["tool_call_id"], tool_msg["content"])

        self.model.tool_call_count += 1
        self.model.data["pending_tool_calls"] = None
        self.model.data["pending_content"] = None

        self.model.needs_input = False

    async def on_enter_done(self):
        """存储记忆，检查护栏，设置最终结果。"""
        memory = self.model.memory
        store = self.model.store
        user_input = self.model.data["user_input"]

        # 工具调用次数超限（正常回复已由 call_model 流式输出）
        if self.model.tool_call_count >= MAX_TOOL_CALLS and not self.model.data.get("final_response"):
            final_response = "抱歉，工具调用次数过多，请稍后重试或简化问题。"
            memory.add_assistant_message({"role": "assistant", "content": final_response})
            self.model.data["final_response"] = final_response
            self.model.output_text = f"\n{OUTPUT_PREFIX}{final_response}\n"

        final_response = self.model.data.get("final_response", "")

        # 存储事实到长期记忆
        await store.add_from_conversation(user_input)

        # 检查是否需要压缩
        if memory.should_compress():
            await memory.compress(store)

        # 输出护栏
        passed, reason = output_guard.check(final_response)
        if not passed:
            final_response = "抱歉，生成的回复包含不安全内容，已过滤。"
            self.model.output_text = f"\n{OUTPUT_PREFIX}{final_response}\n"

        self.model.result = final_response
