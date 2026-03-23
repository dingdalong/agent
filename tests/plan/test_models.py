"""
测试 plan.models 模块
"""
import pytest
from src.plan.models import Step, Plan


def test_step_model():
    """测试 Step 模型的基本验证"""
    # 正常工具步骤
    step = Step(
        id="step1",
        description="查询天气",
        action="tool",
        tool_name="get_weather",
        tool_args={"location": "广州"},
        depends_on=[]
    )
    assert step.id == "step1"
    assert step.action == "tool"
    assert step.tool_name == "get_weather"
    assert step.tool_args == {"location": "广州"}
    assert step.depends_on == []

    # 子任务步骤
    step2 = Step(
        id="step2",
        description="处理子任务",
        action="subtask",
        subtask_prompt="需要进一步规划的任务",
        depends_on=["step1"]
    )
    assert step2.action == "subtask"
    assert step2.subtask_prompt == "需要进一步规划的任务"
    assert step2.depends_on == ["step1"]
    assert step2.tool_name is None
    assert step2.tool_args is None

    # 用户输入步骤
    step3 = Step(
        id="step3",
        description="获取用户偏好",
        action="user_input",
        depends_on=[]
    )
    assert step3.action == "user_input"
    assert step3.tool_name is None
    assert step3.subtask_prompt is None


def test_step_model_defaults():
    """测试 Step 模型的默认值"""
    step = Step(
        id="step1",
        description="测试步骤",
        action="tool",
        tool_name="test_tool"
    )
    assert step.tool_args is None
    assert step.subtask_prompt is None
    assert step.depends_on == []  # default_factory=list


def test_step_model_validation():
    """测试 Step 模型的验证"""
    # 缺少必填字段
    with pytest.raises(ValueError):
        Step(id="step1", description="测试")  # 缺少 action

    # action 必须是有效值（Literal类型限制）
    with pytest.raises(ValueError):
        Step(
            id="step1",
            description="测试",
            action="invalid_action"  # Literal类型会拒绝无效值
        )


def test_plan_model():
    """测试 Plan 模型"""
    step1 = Step(
        id="step1",
        description="第一步",
        action="tool",
        tool_name="test"
    )
    step2 = Step(
        id="step2",
        description="第二步",
        action="user_input",
        depends_on=["step1"]
    )

    plan = Plan(steps=[step1, step2])
    assert len(plan.steps) == 2
    assert plan.steps[0].id == "step1"
    assert plan.steps[1].id == "step2"
    assert plan.context == {}  # 默认空字典


def test_plan_model_with_context():
    """测试带上下文的 Plan 模型"""
    step = Step(
        id="step1",
        description="测试",
        action="tool",
        tool_name="test"
    )
    context = {"user_id": "123", "session_id": "abc"}
    plan = Plan(steps=[step], context=context)
    assert plan.context == context


def test_step_depends_on_validation():
    """测试 depends_on 字段验证"""
    # depends_on 应为字符串列表
    step = Step(
        id="step1",
        description="测试",
        action="tool",
        tool_name="test",
        depends_on=["step2", "step3"]
    )
    assert step.depends_on == ["step2", "step3"]

    # 空列表
    step2 = Step(
        id="step2",
        description="测试2",
        action="tool",
        tool_name="test",
        depends_on=[]
    )
    assert step2.depends_on == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])