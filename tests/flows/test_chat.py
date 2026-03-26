"""
测试 src/flows/chat.py — ChatFlow 状态机
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.flows.chat import ChatFlow, ChatModel, MAX_TOOL_CALLS
from src.core.fsm import FSMRunner, OUTPUT_PREFIX
from src.memory.types import MemoryRecord, MemoryType


def _make_chat_flow(
    user_input="你好",
    fact_records=None,
    summary_records=None,
    call_model_returns=None,
):
    """构造 ChatFlow 并注入 mock 依赖"""
    memory = MagicMock()
    memory.get_messages_for_api.return_value = [
        {"role": "user", "content": user_input}
    ]
    memory.should_compress.return_value = False

    store = MagicMock()
    # search returns MemoryRecord list based on memory_type filter
    def _mock_search(query, n=5, memory_type=None, type_tag=None):
        if memory_type == MemoryType.FACT:
            return fact_records or []
        if memory_type == MemoryType.SUMMARY:
            return summary_records or []
        return (fact_records or []) + (summary_records or [])
    store.search.side_effect = _mock_search
    store.add_from_conversation = AsyncMock()

    tool_executor = AsyncMock()

    flow = ChatFlow(
        memory=memory,
        store=store,
        tools_schema=[],
        tool_executor=tool_executor,
    )
    flow.model.data["user_input"] = user_input
    return flow


# ==================== ChatModel 测试 ====================


class TestChatModel:
    def test_init(self):
        model = ChatModel(
            memory=MagicMock(),
            store=MagicMock(),
            tools_schema=[],
            tool_executor=MagicMock(),
        )
        assert model.tool_call_count == 0
        assert model.data == {}


# ==================== ChatFlow 状态转移测试 ====================


class TestChatFlowTransitions:

    @pytest.mark.asyncio
    async def test_simple_response_no_tools(self):
        """测试普通回复（无工具调用）：retrieving → calling → done"""
        flow = _make_chat_flow(user_input="你好")

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("你好！有什么可以帮你的？", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                result = await runner.run()

        assert result == "你好！有什么可以帮你的？"
        assert flow.model.tool_call_count == 0

    @pytest.mark.asyncio
    async def test_tool_call_loop(self):
        """测试工具调用循环：retrieving → calling → tool_executing → calling → done"""
        flow = _make_chat_flow(user_input="北京天气")

        tool_calls = {0: {"id": "call_1", "name": "get_weather", "arguments": '{"location":"北京"}'}}

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.execute_tool_calls", new_callable=AsyncMock) as mock_exec, \
             patch("src.flows.chat.output_guard") as mock_guard:
            # 第一次调用：返回工具调用
            # 第二次调用：返回最终回复
            mock_call.side_effect = [
                ("", tool_calls, "tool_calls"),
                ("北京今天晴，25°C", None, "stop"),
            ]
            mock_exec.return_value = [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"location":"北京"}'}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "北京：晴，25°C"},
            ]
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                result = await runner.run()

        assert result == "北京今天晴，25°C"
        assert flow.model.tool_call_count == 1

    @pytest.mark.asyncio
    async def test_max_tool_calls_limit(self):
        """测试工具调用次数超限"""
        flow = _make_chat_flow(user_input="复杂任务")

        tool_calls = {0: {"id": "call_1", "name": "tool1", "arguments": '{}'}}

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.execute_tool_calls", new_callable=AsyncMock) as mock_exec, \
             patch("src.flows.chat.output_guard") as mock_guard:
            # 每次都返回工具调用，模拟无限循环
            mock_call.return_value = ("", tool_calls, "tool_calls")
            mock_exec.return_value = [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "tool1", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ]
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                result = await runner.run()

        assert flow.model.tool_call_count == MAX_TOOL_CALLS
        assert "工具调用次数过多" in result

    @pytest.mark.asyncio
    async def test_output_guardrail_blocks(self):
        """测试输出护栏拦截不安全内容"""
        flow = _make_chat_flow(user_input="测试")

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("危险内容", None, "stop")
            mock_guard.check.return_value = (False, "包含不安全内容")

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                result = await runner.run()

        assert "不安全内容" in result

    @pytest.mark.asyncio
    async def test_memory_retrieval_enhances_prompt(self):
        """测试记忆检索结果被加入 system prompt"""
        fact = MemoryRecord(memory_type=MemoryType.FACT, content="用户叫大龙")
        flow = _make_chat_flow(user_input="你好", fact_records=[fact])

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("你好大龙！", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                await runner.run()

        # 验证 system prompt 包含记忆
        system_prompt = flow.model.data["system_prompt"]
        assert "用户叫大龙" in system_prompt

    @pytest.mark.asyncio
    async def test_user_message_added_to_memory(self):
        """测试用户消息被加入短期记忆"""
        flow = _make_chat_flow(user_input="你好世界")

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("回复", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                await runner.run()

        flow.model.memory.add_user_message.assert_called_once_with("你好世界")

    @pytest.mark.asyncio
    async def test_facts_stored_in_long_term_memory(self):
        """测试事实被存储到长期记忆"""
        flow = _make_chat_flow(user_input="我叫大龙")

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("你好大龙！", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                await runner.run()

        flow.model.store.add_from_conversation.assert_called_once_with("我叫大龙")

    @pytest.mark.asyncio
    async def test_memory_compression_triggered(self):
        """测试记忆压缩在需要时触发"""
        flow = _make_chat_flow(user_input="你好")
        flow.model.memory.should_compress.return_value = True
        flow.model.memory.compress = AsyncMock()

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("回复", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                    await runner.run()

        flow.model.memory.compress.assert_called_once_with(flow.model.store)

    @pytest.mark.asyncio
    async def test_output_prefix_set_in_retrieving(self):
        """测试 retrieving 状态设置 OUTPUT_PREFIX"""
        flow = _make_chat_flow(user_input="你好")

        with patch("src.flows.chat.call_model", new_callable=AsyncMock) as mock_call, \
             patch("src.flows.chat.output_guard") as mock_guard:
            mock_call.return_value = ("回复", None, "stop")
            mock_guard.check.return_value = (True, None)

            runner = FSMRunner(flow)
            with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock):
                await runner.run()

        # 第一次输出应该包含 OUTPUT_PREFIX
        first_output = mock_out.call_args_list[0].args[0]
        assert OUTPUT_PREFIX in first_output


# ==================== ChatFlow 条件方法测试 ====================


class TestChatFlowConditions:

    def test_no_tool_calls(self):
        flow = _make_chat_flow()
        flow.model.data["pending_tool_calls"] = None
        assert flow.no_tool_calls() is True

    def test_has_tool_calls(self):
        flow = _make_chat_flow()
        flow.model.data["pending_tool_calls"] = {0: {"id": "c1", "name": "t1", "arguments": "{}"}}
        assert flow.has_tool_calls() is True

    def test_can_continue_tools(self):
        flow = _make_chat_flow()
        flow.model.tool_call_count = 0
        assert flow.can_continue_tools() is True
        flow.model.tool_call_count = MAX_TOOL_CALLS - 1
        assert flow.can_continue_tools() is True

    def test_max_tools_reached(self):
        flow = _make_chat_flow()
        flow.model.tool_call_count = MAX_TOOL_CALLS
        assert flow.max_tools_reached() is True
        flow.model.tool_call_count = MAX_TOOL_CALLS + 1
        assert flow.max_tools_reached() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
