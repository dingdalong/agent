"""
测试 src/core/fsm.py — FlowModel + FSMRunner
"""
import pytest
from unittest.mock import AsyncMock, patch, call

from statemachine import StateMachine, State

from src.core.fsm import FlowModel, FSMRunner, CANCEL_KEYWORDS, OUTPUT_PREFIX


# ==================== FlowModel 测试 ====================


class TestFlowModel:
    def test_defaults(self):
        model = FlowModel()
        assert model.data == {}
        assert model.user_input is None
        assert model.output_text is None
        assert model.needs_input is True
        assert model.result is None
        assert model.is_cancelled is False

    def test_data_storage(self):
        model = FlowModel()
        model.data["key"] = "value"
        model.result = "done"
        assert model.data["key"] == "value"
        assert model.result == "done"


# ==================== 用于测试的简单 Flow ====================


class SimpleModel(FlowModel):
    pass


class TwoStepFlow(StateMachine):
    """两步 flow：asking → done，用于测试 FSMRunner 基本流程"""

    asking = State(initial=True)
    done = State(final=True)

    proceed = asking.to(done)

    def __init__(self):
        model = SimpleModel()
        super().__init__(model=model)

    async def on_enter_asking(self):
        self.model.output_text = f"{OUTPUT_PREFIX}请回答问题：\n"
        self.model.needs_input = True

    async def on_enter_done(self):
        self.model.result = f"收到: {self.model.user_input}"


class AutoFlow(StateMachine):
    """全自动 flow（无需用户输入），用于测试自动转移"""

    step1 = State(initial=True)
    step2 = State()
    done = State(final=True)

    proceed = step1.to(step2) | step2.to(done)

    def __init__(self):
        model = SimpleModel()
        super().__init__(model=model)

    async def on_enter_step1(self):
        self.model.data["step1"] = "完成"
        self.model.output_text = f"{OUTPUT_PREFIX}步骤1完成\n"
        self.model.needs_input = False

    async def on_enter_step2(self):
        self.model.data["step2"] = "完成"
        self.model.needs_input = False

    async def on_enter_done(self):
        self.model.result = "全部完成"


class CancellableFlow(StateMachine):
    """支持取消的 flow"""

    asking = State(initial=True)
    done = State(final=True)
    cancelled = State(final=True)

    proceed = asking.to(done)
    cancel = asking.to(cancelled)

    def __init__(self):
        model = SimpleModel()
        super().__init__(model=model)

    async def on_enter_asking(self):
        self.model.output_text = f"{OUTPUT_PREFIX}请输入（可取消）：\n"
        self.model.needs_input = True

    async def on_enter_done(self):
        self.model.result = self.model.user_input

    async def on_enter_cancelled(self):
        self.model.output_text = f"\n{OUTPUT_PREFIX}已取消。\n"
        self.model.result = "已取消"


class MultiRoundFlow(StateMachine):
    """多轮交互 flow，用于测试多次输入/输出循环"""

    ask_name = State(initial=True)
    ask_age = State()
    done = State(final=True)

    proceed = ask_name.to(ask_age) | ask_age.to(done)

    def __init__(self):
        model = SimpleModel()
        super().__init__(model=model)

    async def on_enter_ask_name(self):
        self.model.output_text = f"{OUTPUT_PREFIX}你叫什么？\n"
        self.model.needs_input = True

    async def on_exit_ask_name(self):
        self.model.data["name"] = self.model.user_input

    async def on_enter_ask_age(self):
        self.model.output_text = f"{OUTPUT_PREFIX}你几岁？\n"
        self.model.needs_input = True

    async def on_exit_ask_age(self):
        self.model.data["age"] = self.model.user_input

    async def on_enter_done(self):
        self.model.result = f"{self.model.data['name']},{self.model.data['age']}"


class NoCancelFlow(StateMachine):
    """不支持 cancel 事件的 flow，用于测试取消关键词在没有 cancel 转移时的行为"""

    asking = State(initial=True)
    done = State(final=True)

    proceed = asking.to(done)

    def __init__(self):
        model = SimpleModel()
        super().__init__(model=model)

    async def on_enter_asking(self):
        self.model.output_text = f"{OUTPUT_PREFIX}请输入：\n"
        self.model.needs_input = True

    async def on_enter_done(self):
        self.model.result = "done"


# ==================== FSMRunner 测试 ====================


class TestFSMRunner:

    @pytest.mark.asyncio
    async def test_basic_two_step(self):
        """测试基本的两步 flow：输出提示 → 用户输入 → 到达终态"""
        flow = TwoStepFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "你好"
            result = await runner.run()

        assert result == "收到: 你好"
        mock_out.assert_any_call(f"{OUTPUT_PREFIX}请回答问题：\n")

    @pytest.mark.asyncio
    async def test_auto_flow_no_input(self):
        """测试全自动 flow，不需要用户输入"""
        flow = AutoFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            result = await runner.run()

        assert result == "全部完成"
        # agent_input 不应被调用（所有状态 needs_input=False）
        mock_in.assert_not_called()
        # 应该输出步骤1的文本
        mock_out.assert_any_call(f"{OUTPUT_PREFIX}步骤1完成\n")

    @pytest.mark.asyncio
    async def test_cancel_with_cancel_event(self):
        """测试取消关键词触发 cancel 事件"""
        flow = CancellableFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "取消"
            result = await runner.run()

        assert result == "已取消"

    @pytest.mark.asyncio
    async def test_cancel_without_cancel_event(self):
        """测试在不支持 cancel 的 flow 中输入取消关键词，应优雅退出"""
        flow = NoCancelFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "取消"
            result = await runner.run()

        # result 为 None 因为没到达 done 状态
        assert result is None
        # 应输出"已取消"的提示
        mock_out.assert_any_call(f"\n{OUTPUT_PREFIX}已取消。\n")

    @pytest.mark.asyncio
    async def test_all_cancel_keywords(self):
        """测试所有取消关键词"""
        for keyword in CANCEL_KEYWORDS:
            flow = CancellableFlow()
            runner = FSMRunner(flow)

            with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
                 patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
                mock_in.return_value = keyword
                result = await runner.run()

            assert result == "已取消", f"关键词 '{keyword}' 未触发取消"

    @pytest.mark.asyncio
    async def test_cancel_keyword_with_whitespace(self):
        """测试取消关键词前后有空格"""
        flow = CancellableFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "  取消  "
            result = await runner.run()

        assert result == "已取消"

    @pytest.mark.asyncio
    async def test_multi_round_interaction(self):
        """测试多轮交互 flow"""
        flow = MultiRoundFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.side_effect = ["大龙", "30"]
            result = await runner.run()

        assert result == "大龙,30"
        assert mock_in.call_count == 2
        # 验证输出了两个提示
        output_texts = [c.args[0] for c in mock_out.call_args_list]
        assert any("你叫什么" in t for t in output_texts)
        assert any("你几岁" in t for t in output_texts)

    @pytest.mark.asyncio
    async def test_output_text_cleared_after_display(self):
        """测试 output_text 在输出后被清空"""
        flow = TwoStepFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock), \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "测试"
            await runner.run()

        # 运行结束后 output_text 应为 None
        assert flow.model.output_text is None

    @pytest.mark.asyncio
    async def test_final_state_output(self):
        """测试终态设置的 output_text 在退出前被输出"""
        flow = CancellableFlow()
        runner = FSMRunner(flow)

        with patch("src.core.fsm.agent_output", new_callable=AsyncMock) as mock_out, \
             patch("src.core.fsm.agent_input", new_callable=AsyncMock) as mock_in:
            mock_in.return_value = "取消"
            await runner.run()

        # 终态 cancelled 设置了 output_text，应该被输出
        output_texts = [c.args[0] for c in mock_out.call_args_list]
        assert any("已取消" in t for t in output_texts)

    @pytest.mark.asyncio
    async def test_runner_model_property(self):
        """测试 FSMRunner.model 属性"""
        flow = TwoStepFlow()
        runner = FSMRunner(flow)
        assert runner.model is flow.model


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
