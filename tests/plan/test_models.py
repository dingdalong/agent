"""测试 plan.models 模块"""
import pytest
from src.plan.models import Step, Plan


class TestStepModel:
    def test_tool_step(self):
        """工具步骤：tool_name 有值"""
        step = Step(
            id="weather",
            description="查询天气",
            tool_name="get_weather",
            tool_args={"location": "广州"},
        )
        assert step.id == "weather"
        assert step.tool_name == "get_weather"
        assert step.tool_args == {"location": "广州"}
        assert step.agent_name is None
        assert step.agent_prompt is None
        assert step.depends_on == []

    def test_agent_step(self):
        """Agent 步骤：agent_name 有值"""
        step = Step(
            id="draft",
            description="起草邮件",
            agent_name="email_agent",
            agent_prompt="根据天气信息起草一封邮件",
            depends_on=["weather"],
        )
        assert step.agent_name == "email_agent"
        assert step.agent_prompt == "根据天气信息起草一封邮件"
        assert step.tool_name is None
        assert step.depends_on == ["weather"]

    def test_tool_step_defaults(self):
        """工具步骤默认值"""
        step = Step(id="s1", description="测试", tool_name="test_tool")
        assert step.tool_args == {}
        assert step.depends_on == []

    def test_agent_step_defaults(self):
        """Agent 步骤默认值"""
        step = Step(id="s1", description="测试", agent_name="helper")
        assert step.agent_prompt is None
        assert step.depends_on == []

    def test_both_tool_and_agent_raises(self):
        """同时设置 tool_name 和 agent_name 报错"""
        with pytest.raises(ValueError, match="cannot have both"):
            Step(
                id="s1",
                description="冲突",
                tool_name="some_tool",
                agent_name="some_agent",
            )

    def test_neither_tool_nor_agent_raises(self):
        """两者都没设置报错"""
        with pytest.raises(ValueError, match="must have either"):
            Step(id="s1", description="空步骤")

    def test_variable_references_in_tool_args(self):
        """tool_args 中的 $step_id.field 变量引用"""
        step = Step(
            id="translate",
            description="翻译",
            tool_name="translate",
            tool_args={"text": "$search.results", "lang": "zh"},
            depends_on=["search"],
        )
        assert step.tool_args["text"] == "$search.results"


class TestPlanModel:
    def test_basic_plan(self):
        plan = Plan(steps=[
            Step(id="s1", description="步骤1", tool_name="t1"),
            Step(id="s2", description="步骤2", tool_name="t2", depends_on=["s1"]),
        ])
        assert len(plan.steps) == 2
        assert plan.context == {}

    def test_plan_with_context(self):
        plan = Plan(
            steps=[Step(id="s1", description="测试", tool_name="t1")],
            context={"user_id": "123"},
        )
        assert plan.context == {"user_id": "123"}
