"""
测试 plan.planner 模块
"""
import pytest
import json
from unittest.mock import AsyncMock, patch
from src.plan.planner import generate_plan, adjust_plan
from src.plan.models import Plan, Step


@pytest.mark.asyncio
async def test_generate_plan_success():
    """测试成功生成计划"""
    # 模拟可用工具
    available_tools = [
        {
            "function": {
                "name": "get_weather",
                "description": "获取天气信息"
            }
        },
        {
            "function": {
                "name": "calculate",
                "description": "执行计算"
            }
        }
    ]

    # 模拟 API 响应
    mock_response = json.dumps({
        "steps": [
            {
                "id": "step1",
                "description": "查询天气",
                "action": "tool",
                "tool_name": "get_weather",
                "tool_args": {"location": "广州"},
                "depends_on": []
            },
            {
                "id": "step2",
                "description": "计算结果",
                "action": "tool",
                "tool_name": "calculate",
                "tool_args": {"expression": "2+2"},
                "depends_on": ["step1"]
            }
        ]
    })

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (mock_response, {}, {})

        plan = await generate_plan(
            user_input="查询广州天气并计算2+2",
            available_tools=available_tools
        )

        # 验证调用
        assert mock_call.called
        call_args = mock_call.call_args[0][0]
        assert isinstance(call_args, list)
        assert call_args[0]["role"] == "system"
        assert call_args[1]["role"] == "user"

        # 验证结果
        # 使用属性检查而不是 isinstance，避免导入问题
        assert hasattr(plan, 'steps') and hasattr(plan, 'context')
        assert len(plan.steps) == 2
        assert plan.steps[0].id == "step1"
        assert plan.steps[0].action == "tool"
        assert plan.steps[0].tool_name == "get_weather"
        assert plan.steps[1].id == "step2"
        assert plan.steps[1].depends_on == ["step1"]


@pytest.mark.asyncio
async def test_generate_plan_json_extraction():
    """测试 JSON 提取失败时抛出异常"""
    from src.plan.exceptions import JSONParseError
    available_tools = []

    # 模拟返回无效 JSON
    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("无效的响应", {}, {})

        with pytest.raises(JSONParseError):
            await generate_plan(
                user_input="测试请求",
                available_tools=available_tools
            )


@pytest.mark.asyncio
async def test_generate_plan_empty_steps():
    """测试返回空步骤列表"""
    available_tools = []

    mock_response = json.dumps({
        "steps": []  # 空步骤
    })

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (mock_response, {}, {})

        plan = await generate_plan(
            user_input="测试请求",
            available_tools=available_tools
        )

        assert hasattr(plan, 'steps') and hasattr(plan, 'context')
        assert len(plan.steps) == 0


@pytest.mark.asyncio
async def test_adjust_plan_success():
    """测试成功调整计划"""
    available_tools = [
        {
            "function": {
                "name": "test_tool",
                "description": "测试工具"
            }
        }
    ]

    original_plan = Plan(steps=[
        Step(
            id="step1",
            description="原始步骤1",
            action="tool",
            tool_name="test_tool"
        )
    ])

    # 模拟调整后的响应
    mock_response = json.dumps({
        "steps": [
            {
                "id": "step1",
                "description": "修改后的步骤1",
                "action": "tool",
                "tool_name": "test_tool",
                "tool_args": {"param": "value"},
                "depends_on": []
            },
            {
                "id": "step2",
                "description": "新增步骤2",
                "action": "user_input",
                "depends_on": ["step1"]
            }
        ]
    })

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (mock_response, {}, {})

        new_plan = await adjust_plan(
            original_request="原始请求",
            current_plan=original_plan,
            feedback="添加一个新步骤",
            available_tools=available_tools
        )

        assert hasattr(new_plan, 'steps') and hasattr(new_plan, 'context')
        assert len(new_plan.steps) == 2
        assert new_plan.steps[0].description == "修改后的步骤1"
        assert new_plan.steps[1].id == "step2"
        assert new_plan.steps[1].action == "user_input"


@pytest.mark.asyncio
async def test_adjust_plan_failure():
    """测试调整计划失败时返回原计划"""
    original_plan = Plan(steps=[
        Step(
            id="step1",
            description="原始步骤",
            action="tool",
            tool_name="test_tool"
        )
    ])

    # 模拟无效响应
    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("无效响应", {}, {})

        new_plan = await adjust_plan(
            original_request="测试",
            current_plan=original_plan,
            feedback="修改",
            available_tools=[]
        )

        # 应该返回原计划
        assert new_plan == original_plan
        assert len(new_plan.steps) == 1
        assert new_plan.steps[0].description == "原始步骤"


@pytest.mark.asyncio
async def test_adjust_plan_with_tools_description():
    """测试工具描述是否正确构建"""
    available_tools = [
        {
            "function": {
                "name": "tool1",
                "description": "工具1描述"
            }
        },
        {
            "function": {
                "name": "tool2",
                "description": "工具2描述"
            }
        }
    ]

    original_plan = Plan(steps=[])

    mock_response = json.dumps({"steps": []})

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (mock_response, {}, {})

        await adjust_plan(
            original_request="测试",
            current_plan=original_plan,
            feedback="无修改",
            available_tools=available_tools
        )

        # 验证工具描述被包含在系统提示中
        call_args = mock_call.call_args[0][0]
        system_prompt = call_args[0]["content"]
        assert "工具1描述" in system_prompt
        assert "工具2描述" in system_prompt


@pytest.mark.asyncio
async def test_generate_plan_with_context():
    """测试带上下文的计划生成"""
    available_tools = []

    mock_response = json.dumps({"steps": []})

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (mock_response, {}, {})

        await generate_plan(
            user_input="测试请求",
            available_tools=available_tools,
            context="额外上下文信息"
        )

        # 验证上下文被包含在用户提示中
        call_args = mock_call.call_args[0][0]
        user_prompt = call_args[1]["content"]
        assert "额外上下文信息" in user_prompt


@pytest.mark.asyncio
async def test_generate_plan_timeout():
    """测试计划生成超时"""
    import asyncio

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(100)
        return ("", {}, {})

    with patch('src.plan.planner.call_model', side_effect=slow_call), \
         patch('src.plan.planner.PLAN_GENERATION_TIMEOUT', 0.01):
        with pytest.raises(Exception) as exc_info:
            await generate_plan("测试", [])
        assert "超时" in str(exc_info.value)


@pytest.mark.asyncio
async def test_adjust_plan_timeout():
    """测试计划调整超时返回原计划"""
    import asyncio

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(100)
        return ("", {}, {})

    original_plan = Plan(steps=[
        Step(id="s1", description="原始", action="tool", tool_name="t")
    ])

    with patch('src.plan.planner.call_model', side_effect=slow_call), \
         patch('src.plan.planner.PLAN_GENERATION_TIMEOUT', 0.01):
        result = await adjust_plan("测试", original_plan, "反馈", [])
        # 超时应返回原计划
        assert result == original_plan


if __name__ == "__main__":
    pytest.main([__file__, "-v"])