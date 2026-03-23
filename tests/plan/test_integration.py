"""
测试 plan.integration 模块
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.plan.integration import handle_planning_request
from src.plan.models import Plan, Step


@pytest.mark.asyncio
async def test_handle_planning_request_immediate_confirmation():
    """测试用户立即确认计划"""
    available_tools = [{"function": {"name": "test", "description": "测试工具"}}]
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    # 模拟 generate_plan 返回一个简单计划
    mock_plan = Plan(steps=[
        Step(id="step1", description="测试步骤", action="tool", tool_name="test")
    ])

    # 模拟 execute_plan 返回结果
    mock_result = {"step1": "执行结果"}

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.execute_plan', new_callable=AsyncMock) as mock_exec, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_exec.return_value = mock_result
        mock_classify.return_value = "confirm"
        mock_input_func.return_value = "确认"

        result = await handle_planning_request(
            user_input="测试请求",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func,
            max_adjustments=3
        )

        # 验证计划生成被调用
        mock_gen.assert_called_once_with("测试请求", available_tools)

        # 验证 LLM 分类被调用
        mock_classify.assert_called_once_with("确认", mock_plan)

        # 验证执行计划被调用
        mock_exec.assert_called_once_with(mock_plan, mock_executor, mock_input_func)

        # 验证结果格式化
        assert "测试步骤" in result
        assert "执行结果" in result


@pytest.mark.asyncio
async def test_handle_planning_request_with_adjustment():
    """测试用户调整计划后再确认"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    original_plan = Plan(steps=[
        Step(id="step1", description="原始步骤", action="tool", tool_name="test")
    ])

    adjusted_plan = Plan(steps=[
        Step(id="step1", description="调整后步骤", action="tool", tool_name="test"),
        Step(id="step2", description="新增步骤", action="user_input")
    ])

    mock_result = {"step1": "结果1", "step2": "结果2"}

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.adjust_plan', new_callable=AsyncMock) as mock_adj, \
         patch('src.plan.integration.execute_plan', new_callable=AsyncMock) as mock_exec, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = original_plan
        mock_adj.return_value = adjusted_plan
        mock_exec.return_value = mock_result
        # 第一轮：调整；第二轮：确认
        mock_classify.side_effect = ["adjust", "confirm"]

        mock_input_func.side_effect = ["修改：添加一个步骤", "确认"]

        result = await handle_planning_request(
            user_input="测试请求",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func,
            max_adjustments=3
        )

        # 验证调整计划被调用
        mock_adj.assert_called_once_with(
            "测试请求", original_plan, "修改：添加一个步骤", available_tools
        )

        # 验证执行了调整后的计划
        mock_exec.assert_called_once_with(adjusted_plan, mock_executor, mock_input_func)

        # 结果应包含两个步骤
        assert "调整后步骤" in result
        assert "新增步骤" in result


@pytest.mark.asyncio
async def test_handle_planning_request_max_adjustments():
    """测试达到最大调整次数"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    mock_plan = Plan(steps=[
        Step(id="step1", description="最终计划", action="tool", tool_name="test")
    ])

    mock_result = {"step1": "结果"}

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.adjust_plan', new_callable=AsyncMock) as mock_adj, \
         patch('src.plan.integration.execute_plan', new_callable=AsyncMock) as mock_exec, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_adj.return_value = mock_plan
        mock_exec.return_value = mock_result
        # 3次都判断为调整
        mock_classify.return_value = "adjust"

        mock_input_func.side_effect = ["修改1", "修改2", "修改3", "y"]

        result = await handle_planning_request(
            user_input="测试请求",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func,
            max_adjustments=3
        )

        assert mock_adj.call_count == 3
        mock_exec.assert_called_once()
        assert "最终计划" in result


@pytest.mark.asyncio
async def test_handle_planning_request_cancel_after_max_adjustments():
    """测试达到最大调整次数后取消"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    mock_plan = Plan(steps=[
        Step(id="step1", description="测试步骤", action="tool", tool_name="test")
    ])

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.adjust_plan', new_callable=AsyncMock) as mock_adj, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_adj.return_value = mock_plan
        mock_classify.return_value = "adjust"

        mock_input_func.side_effect = ["修改1", "修改2", "修改3", "n"]

        result = await handle_planning_request(
            user_input="测试请求",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func,
            max_adjustments=3
        )

        assert result == "计划已取消。"


@pytest.mark.asyncio
async def test_handle_planning_request_empty_plan():
    """测试模型判断不需要计划的情况（generate_plan 返回 None）"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen:
        # generate_plan 对空步骤返回 None（表示不需要计划）
        mock_gen.return_value = None

        result = await handle_planning_request(
            user_input="测试请求",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func
        )

        # 返回 None 表示回退到普通对话
        assert result is None
        # 不应该调用输入函数
        mock_input_func.assert_not_called()


@pytest.mark.asyncio
async def test_handle_planning_request_llm_classification():
    """测试LLM分类确认和调整"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    mock_plan = Plan(steps=[
        Step(id="step1", description="测试", action="tool", tool_name="test")
    ])
    mock_result = {"step1": "结果"}

    # 测试各种自然语言确认
    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.execute_plan', new_callable=AsyncMock) as mock_exec, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_exec.return_value = mock_result
        mock_classify.return_value = "confirm"
        mock_input_func.return_value = "没问题，就这样吧"

        result = await handle_planning_request(
            user_input="测试",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func
        )

        mock_classify.assert_called_once_with("没问题，就这样吧", mock_plan)
        mock_exec.assert_called_once()
        assert "测试" in result


@pytest.mark.asyncio
async def test_handle_planning_request_result_formatting():
    """测试结果格式化"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    # 创建包含多个步骤的计划
    steps = [
        Step(id="step1", description="步骤一", action="tool", tool_name="tool1"),
        Step(id="step2", description="步骤二", action="tool", tool_name="tool2"),
        Step(id="step3", description="步骤三", action="user_input")
    ]
    mock_plan = Plan(steps=steps)

    # 模拟执行结果，其中 step3 没有结果
    mock_result = {
        "step1": "结果1",
        "step2": "结果2"
        # step3 没有结果
    }

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.execute_plan', new_callable=AsyncMock) as mock_exec, \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_exec.return_value = mock_result
        mock_classify.return_value = "confirm"
        mock_input_func.return_value = "确认"

        result = await handle_planning_request(
            user_input="测试",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func
        )

        lines = result.split('\n')
        assert len(lines) == 3
        assert "步骤一: 结果1" in result
        assert "步骤二: 结果2" in result
        assert "步骤三: 无结果" in result


@pytest.mark.asyncio
async def test_handle_planning_request_default_max_adjustments():
    """测试默认最大调整次数"""
    available_tools = []
    mock_executor = MagicMock()
    mock_input_func = AsyncMock()

    mock_plan = Plan(steps=[Step(id="step1", description="测试", action="tool", tool_name="test")])

    with patch('src.plan.integration.generate_plan', new_callable=AsyncMock) as mock_gen, \
         patch('src.plan.integration.adjust_plan', new_callable=AsyncMock), \
         patch('src.plan.integration.classify_user_feedback', new_callable=AsyncMock) as mock_classify:

        mock_gen.return_value = mock_plan
        mock_classify.return_value = "adjust"

        mock_input_func.side_effect = ["修改1", "修改2", "修改3", "修改4", "n"]

        result = await handle_planning_request(
            user_input="测试",
            available_tools=available_tools,
            tool_executor=mock_executor,
            async_input_func=mock_input_func
        )

        assert result == "计划已取消。"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])