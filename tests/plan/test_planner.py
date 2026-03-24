"""
测试 plan.planner 模块
"""
import pytest
import json
from unittest.mock import AsyncMock, patch
from src.plan.planner import generate_plan, adjust_plan, parse_plan_from_tool_calls, build_submit_plan_schema
from src.plan.models import Plan, Step


def _make_submit_plan_tool_calls(steps_data: list) -> dict:
    """构造 submit_plan 的 tool_calls 返回格式"""
    return {
        0: {
            "id": "call_test",
            "name": "submit_plan",
            "arguments": json.dumps({"steps": steps_data}, ensure_ascii=False)
        }
    }


@pytest.mark.asyncio
async def test_generate_plan_success():
    """测试成功生成计划（通过 function calling）"""
    available_tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取天气信息",
                "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "执行计算",
                "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}
            }
        }
    ]

    steps_data = [
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

    tool_calls = _make_submit_plan_tool_calls(steps_data)

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("", tool_calls, "stop")

        plan = await generate_plan(
            user_input="查询广州天气并计算2+2",
            available_tools=available_tools
        )

        # 验证调用传了 tools 参数
        assert mock_call.called
        call_kwargs = mock_call.call_args[1]
        assert "tools" in call_kwargs
        # 应包含原始工具 + submit_plan
        tool_names = [t["function"]["name"] for t in call_kwargs["tools"]]
        assert "get_weather" in tool_names
        assert "calculate" in tool_names
        assert "submit_plan" in tool_names

        # 验证结果
        assert hasattr(plan, 'steps') and hasattr(plan, 'context')
        assert len(plan.steps) == 2
        assert plan.steps[0].id == "step1"
        assert plan.steps[0].action == "tool"
        assert plan.steps[0].tool_name == "get_weather"
        assert plan.steps[0].tool_args == {"location": "广州"}
        assert plan.steps[1].id == "step2"
        assert plan.steps[1].depends_on == ["step1"]


@pytest.mark.asyncio
async def test_generate_plan_no_tool_call():
    """测试 LLM 判断不需要计划（不调用 submit_plan）时返回 None"""
    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("这是一个简单问题，不需要计划。", {}, "stop")

        plan = await generate_plan(
            user_input="你好",
            available_tools=[]
        )

        assert plan is None


@pytest.mark.asyncio
async def test_generate_plan_empty_steps():
    """测试返回空步骤列表"""
    tool_calls = _make_submit_plan_tool_calls([])

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("", tool_calls, "stop")

        plan = await generate_plan(
            user_input="测试请求",
            available_tools=[]
        )

        assert plan is None


@pytest.mark.asyncio
async def test_generate_plan_invalid_tool_call_args():
    """测试 submit_plan 参数解析失败时抛出异常"""
    from src.plan.exceptions import JSONParseError

    tool_calls = {0: {"id": "call_test", "name": "submit_plan", "arguments": "无效的JSON"}}

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("", tool_calls, "stop")

        with pytest.raises(JSONParseError):
            await generate_plan(
                user_input="测试请求",
                available_tools=[]
            )


@pytest.mark.asyncio
async def test_adjust_plan_success():
    """测试成功调整计划"""
    available_tools = [
        {
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "测试工具",
                "parameters": {"type": "object", "properties": {"param": {"type": "string"}}, "required": ["param"]}
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

    adjusted_steps = [
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

    tool_calls = _make_submit_plan_tool_calls(adjusted_steps)

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("", tool_calls, "stop")

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

    # LLM 没调用 submit_plan，返回文本
    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("无法调整", {}, "stop")

        new_plan = await adjust_plan(
            original_request="测试",
            current_plan=original_plan,
            feedback="修改",
            available_tools=[]
        )

        assert new_plan == original_plan
        assert len(new_plan.steps) == 1
        assert new_plan.steps[0].description == "原始步骤"


@pytest.mark.asyncio
async def test_adjust_plan_tools_passed():
    """测试调整计划时工具 schema 通过 tools 参数传递"""
    available_tools = [
        {
            "type": "function",
            "function": {
                "name": "tool1",
                "description": "工具1描述",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "tool2",
                "description": "工具2描述",
                "parameters": {"type": "object", "properties": {}}
            }
        }
    ]

    original_plan = Plan(steps=[])
    tool_calls = _make_submit_plan_tool_calls([])

    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("", tool_calls, "stop")

        await adjust_plan(
            original_request="测试",
            current_plan=original_plan,
            feedback="无修改",
            available_tools=available_tools
        )

        # 验证工具 schema 通过 tools 参数传递
        call_kwargs = mock_call.call_args[1]
        assert "tools" in call_kwargs
        tool_names = [t["function"]["name"] for t in call_kwargs["tools"]]
        assert "tool1" in tool_names
        assert "tool2" in tool_names
        assert "submit_plan" in tool_names


@pytest.mark.asyncio
async def test_generate_plan_with_context():
    """测试带上下文的计划生成"""
    with patch('src.plan.planner.call_model', new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("不需要计划", {}, "stop")

        await generate_plan(
            user_input="测试请求",
            available_tools=[],
            context="额外上下文信息"
        )

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
        assert result == original_plan


# === 单元测试：解析函数 ===

class TestParseplanFromToolCalls:
    def test_parse_valid(self):
        tool_calls = _make_submit_plan_tool_calls([
            {"id": "s1", "description": "步骤1", "action": "tool", "tool_name": "t1"}
        ])
        plan = parse_plan_from_tool_calls(tool_calls)
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].id == "s1"

    def test_parse_no_submit_plan(self):
        tool_calls = {0: {"id": "call_x", "name": "other_tool", "arguments": "{}"}}
        plan = parse_plan_from_tool_calls(tool_calls)
        assert plan is None

    def test_parse_empty_tool_calls(self):
        plan = parse_plan_from_tool_calls({})
        assert plan is None

    def test_parse_invalid_json(self):
        from src.plan.exceptions import JSONParseError
        tool_calls = {0: {"id": "call_x", "name": "submit_plan", "arguments": "not json"}}
        with pytest.raises(JSONParseError):
            parse_plan_from_tool_calls(tool_calls)


class TestBuildSubmitPlanSchema:
    def test_schema_structure(self):
        schema = build_submit_plan_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "submit_plan"
        params = schema["function"]["parameters"]
        assert "steps" in params["properties"]
        assert params["properties"]["steps"]["type"] == "array"

    def test_step_schema_has_required_fields(self):
        schema = build_submit_plan_schema()
        step_schema = schema["function"]["parameters"]["properties"]["steps"]["items"]
        assert "id" in step_schema["properties"]
        assert "description" in step_schema["properties"]
        assert "action" in step_schema["properties"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
