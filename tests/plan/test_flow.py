"""Tests for PlanFlow orchestration."""
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from src.plan.flow import PlanFlow
from src.plan.models import Plan, Step


def _make_flow(ui=None):
    """Create a PlanFlow with mocked dependencies."""
    return PlanFlow(
        tool_router=Mock(get_all_schemas=Mock(return_value=[])),
        agent_registry=Mock(all_agents=Mock(return_value=[])),
        engine=Mock(),
        ui=ui or AsyncMock(),
        llm=AsyncMock(),
    )


class TestFormatPlan:

    def test_tool_step(self):
        plan = Plan(steps=[
            Step(id="s1", description="查询天气", tool_name="get_weather", tool_args={"city": "广州"}),
        ])
        result = PlanFlow.format_plan(plan)
        assert "[工具]" in result
        assert "get_weather" in result

    def test_agent_step(self):
        plan = Plan(steps=[
            Step(id="s1", description="发邮件", agent_name="email_agent"),
        ])
        result = PlanFlow.format_plan(plan)
        assert "[Agent]" in result
        assert "email_agent" in result

    def test_step_with_deps(self):
        plan = Plan(steps=[
            Step(id="s1", description="查询天气", tool_name="get_weather", tool_args={}),
            Step(id="s2", description="发邮件", agent_name="email_agent", depends_on=["s1"]),
        ])
        result = PlanFlow.format_plan(plan)
        assert "依赖" in result
        assert "s1" in result


class TestPlanFlowRun:

    @pytest.mark.asyncio
    async def test_returns_string_when_no_plan_needed(self):
        flow = _make_flow()
        with patch("src.plan.flow.check_clarification_needed", new_callable=AsyncMock, return_value=None), \
             patch("src.plan.flow.generate_plan", new_callable=AsyncMock, return_value=None):
            result = await flow.run("你好")
            assert isinstance(result, str)
            assert "不需要" in result

    @pytest.mark.asyncio
    async def test_uses_ui_for_clarification(self):
        ui = AsyncMock()
        ui.prompt = AsyncMock(return_value="广州")
        flow = _make_flow(ui=ui)

        with patch("src.plan.flow.check_clarification_needed", new_callable=AsyncMock, side_effect=[
            "请问是哪个城市？",  # 第一轮需要澄清
            None,               # 第二轮信息充足
        ]), \
             patch("src.plan.flow.generate_plan", new_callable=AsyncMock, return_value=None):
            await flow.run("查天气")
            ui.display.assert_called()  # 展示了澄清问题
            ui.prompt.assert_called()   # 请求了用户输入
