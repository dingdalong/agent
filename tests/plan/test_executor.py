"""
测试 plan.executor 模块
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.plan.executor import resolve_variables, execute_step, execute_plan, validate_plan, topological_sort_layered
from src.plan.models import Step, Plan
from src.plan.exceptions import StepExecutionError, PlanValidationError, DependencyError


def test_resolve_variables_dict():
    """测试解析字典中的变量"""
    context = {
        "step1": {"result": "天气晴朗", "temp": 25},
        "step2": "简单结果"
    }

    obj = {
        "message": "$step1.result",  # 变量引用必须是整个字符串
        "data": {"value": "$step1.temp"},
        "nested": {"ref": "$step2"}
    }

    result = resolve_variables(obj, context)
    assert result["message"] == "天气晴朗"
    assert result["data"]["value"] == 25
    assert result["nested"]["ref"] == "简单结果"


def test_resolve_variables_list():
    """测试解析列表中的变量"""
    context = {
        "step1": ["a", "b", "c"],
        "step2": {"key": "value"}
    }

    obj = ["$step1", "$step2.key", "literal"]
    result = resolve_variables(obj, context)
    assert result[0] == ["a", "b", "c"]
    assert result[1] == "value"
    assert result[2] == "literal"


def test_resolve_variables_string():
    """测试解析字符串变量"""
    context = {
        "step1": {"result": "成功"},
        "step2": "直接字符串"
    }

    # 简单变量
    assert resolve_variables("$step1.result", context) == "成功"
    assert resolve_variables("$step2", context) == "直接字符串"

    # 嵌套路径
    context["step3"] = {"data": {"nested": {"value": 100}}}
    assert resolve_variables("$step3.data.nested.value", context) == 100

    # 非变量字符串
    assert resolve_variables("普通字符串", context) == "普通字符串"
    assert resolve_variables("$invalid", context) == "$invalid"  # 不存在


def test_resolve_variables_missing_path():
    """测试解析缺失的路径"""
    context = {"step1": {"a": 1}}

    # 中间路径不存在
    assert resolve_variables("$step1.b.c", context) == "$step1.b.c"

    # 路径以字符串结尾
    context["step2"] = "string_value"
    assert resolve_variables("$step2.prop", context) == "$step2.prop"


def test_resolve_variables_edge_cases():
    """测试边缘情况"""
    # 空上下文
    assert resolve_variables("$anything", {}) == "$anything"

    # None 值
    assert resolve_variables(None, {"step": 1}) is None

    # 数字
    assert resolve_variables(123, {}) == 123

    # 布尔值
    assert resolve_variables(True, {}) is True


@pytest.mark.asyncio
async def test_execute_step_tool():
    """测试执行工具步骤"""
    step = Step(
        id="step1",
        description="测试工具",
        action="tool",
        tool_name="test_tool",
        tool_args={"param": "$context.value", "literal": "text"}
    )

    context = {"context": {"value": "动态值"}}
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "工具执行结果"

    result = await execute_step(step, context, mock_executor)

    # 验证工具执行器被调用，参数已解析
    mock_executor.execute.assert_called_once_with(
        "test_tool",
        {"param": "动态值", "literal": "text"}
    )
    assert result == "工具执行结果"


@pytest.mark.asyncio
async def test_execute_step_tool_missing_name():
    """测试缺少工具名的步骤"""
    step = Step(
        id="step1",
        description="测试",
        action="tool",
        # 缺少 tool_name
    )

    with pytest.raises(StepExecutionError) as exc_info:
        await execute_step(step, {}, AsyncMock())

    assert "工具步骤缺少 tool_name" in str(exc_info.value)
    assert step.id in str(exc_info.value)
    assert step.description in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_step_user_input():
    """测试执行用户输入步骤"""
    step = Step(
        id="step1",
        description="请输入信息",
        action="user_input"
    )

    mock_input_func = AsyncMock()
    mock_input_func.return_value = "用户输入内容"

    result = await execute_step(
        step, {}, MagicMock(), async_input_func=mock_input_func
    )

    mock_input_func.assert_called_once_with("请输入信息")
    assert result == "用户输入内容"


@pytest.mark.asyncio
async def test_execute_step_user_input_no_function():
    """测试没有输入函数的用户输入步骤"""
    step = Step(
        id="step1",
        description="请输入",
        action="user_input"
    )

    with pytest.raises(StepExecutionError) as exc_info:
        await execute_step(step, {}, MagicMock(), async_input_func=None)

    assert "需要用户输入，但未提供输入函数" in str(exc_info.value)
    assert step.id in str(exc_info.value)
    assert step.description in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_step_subtask():
    """测试执行子任务步骤"""
    step = Step(
        id="step1",
        description="处理子任务",
        action="subtask",
        subtask_prompt="需要规划的任务"
    )

    result = await execute_step(step, {}, MagicMock())
    assert "子任务未实现" in result
    assert "需要规划的任务" in result


def test_step_rejects_unknown_action():
    """测试 Step 模型拒绝未知动作类型"""
    with pytest.raises(ValueError):
        Step(
            id="step1",
            description="测试",
            action="invalid_action"
        )


@pytest.mark.asyncio
async def test_execute_plan_sequential():
    """测试顺序执行计划"""
    steps = [
        Step(
            id="step1",
            description="第一步",
            action="tool",
            tool_name="tool1"
        ),
        Step(
            id="step2",
            description="第二步",
            action="tool",
            tool_name="tool2",
            depends_on=["step1"]
        )
    ]
    plan = Plan(steps=steps)

    mock_executor = AsyncMock()
    # 为不同工具返回不同结果
    mock_executor.execute.side_effect = ["结果1", "结果2"]

    result = await execute_plan(plan, mock_executor)

    # 验证两个工具都被调用
    assert mock_executor.execute.call_count == 2
    calls = mock_executor.execute.call_args_list
    assert calls[0][0][0] == "tool1"
    assert calls[1][0][0] == "tool2"

    # 验证结果字典
    assert result["step1"] == "结果1"
    assert result["step2"] == "结果2"


@pytest.mark.asyncio
async def test_execute_plan_with_user_input():
    """测试包含用户输入的计划执行"""
    steps = [
        Step(
            id="step1",
            description="获取输入",
            action="user_input"
        ),
        Step(
            id="step2",
            description="使用输入",
            action="tool",
            tool_name="process"
        )
    ]
    plan = Plan(steps=steps)

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "处理结果"

    mock_input_func = AsyncMock()
    mock_input_func.return_value = "用户提供的数据"

    result = await execute_plan(
        plan, mock_executor, async_input_func=mock_input_func
    )

    mock_input_func.assert_called_once_with("获取输入")
    mock_executor.execute.assert_called_once_with("process", {})
    assert result["step1"] == "用户提供的数据"
    assert result["step2"] == "处理结果"


@pytest.mark.asyncio
async def test_execute_plan_variable_chaining():
    """测试变量链式引用"""
    steps = [
        Step(
            id="step1",
            description="第一步",
            action="tool",
            tool_name="tool1",
            tool_args={"output": "data1"}
        ),
        Step(
            id="step2",
            description="第二步",
            action="tool",
            tool_name="tool2",
            tool_args={"input": "$step1"},
            depends_on=["step1"]
        )
    ]
    plan = Plan(steps=steps)

    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = ["结果1", "结果2"]

    result = await execute_plan(plan, mock_executor)

    # 第二步应该接收到第一步的结果作为参数
    calls = mock_executor.execute.call_args_list
    assert calls[1][0][0] == "tool2"
    # 参数应该是解析后的值
    assert calls[1][0][1] == {"input": "结果1"}

    assert result["step1"] == "结果1"
    assert result["step2"] == "结果2"


def test_resolve_variables_depth_limit():
    """测试变量路径深度限制"""
    context = {"a": {"b": {"c": {"d": "value"}}}}
    # 构造超长路径
    deep_path = "$" + ".".join([f"level{i}" for i in range(15)])
    result = resolve_variables(deep_path, context)
    # 超过 MAX_VARIABLE_DEPTH 应返回原始字符串
    assert result == deep_path


def test_validate_plan_duplicate_ids():
    """测试计划验证：重复步骤ID"""
    plan = Plan(steps=[
        Step(id="step1", description="步骤1", action="tool", tool_name="t1"),
        Step(id="step1", description="步骤2", action="tool", tool_name="t2"),
    ])
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan)
    assert "重复的步骤ID" in str(exc_info.value)


def test_validate_plan_tool_missing_name():
    """测试计划验证：tool类型缺少tool_name"""
    plan = Plan(steps=[
        Step(id="step1", description="步骤1", action="tool"),
    ])
    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan)
    assert "缺少 tool_name" in str(exc_info.value)


def test_validate_plan_valid():
    """测试计划验证：合法计划"""
    plan = Plan(steps=[
        Step(id="step1", description="步骤1", action="tool", tool_name="t1"),
        Step(id="step2", description="步骤2", action="user_input"),
    ])
    # 不应抛出异常
    validate_plan(plan)


def test_topological_sort_layered_basic():
    """测试分层拓扑排序：基本场景"""
    steps = [
        Step(id="a", description="A", action="tool", tool_name="t"),
        Step(id="b", description="B", action="tool", tool_name="t"),
        Step(id="c", description="C", action="tool", tool_name="t", depends_on=["a", "b"]),
    ]
    layers = topological_sort_layered(steps)
    assert len(layers) == 2
    # 第一层应包含 a 和 b（无依赖）
    layer0_ids = {s.id for s in layers[0]}
    assert layer0_ids == {"a", "b"}
    # 第二层应包含 c
    assert layers[1][0].id == "c"


def test_topological_sort_layered_linear():
    """测试分层拓扑排序：线性依赖"""
    steps = [
        Step(id="s1", description="1", action="tool", tool_name="t"),
        Step(id="s2", description="2", action="tool", tool_name="t", depends_on=["s1"]),
        Step(id="s3", description="3", action="tool", tool_name="t", depends_on=["s2"]),
    ]
    layers = topological_sort_layered(steps)
    assert len(layers) == 3
    assert layers[0][0].id == "s1"
    assert layers[1][0].id == "s2"
    assert layers[2][0].id == "s3"


def test_topological_sort_layered_cycle():
    """测试分层拓扑排序：循环依赖"""
    steps = [
        Step(id="a", description="A", action="tool", tool_name="t", depends_on=["b"]),
        Step(id="b", description="B", action="tool", tool_name="t", depends_on=["a"]),
    ]
    with pytest.raises(DependencyError):
        topological_sort_layered(steps)


@pytest.mark.asyncio
async def test_execute_plan_parallel():
    """测试并行执行无依赖的步骤"""
    import time

    steps = [
        Step(id="a", description="A", action="tool", tool_name="t1"),
        Step(id="b", description="B", action="tool", tool_name="t2"),
        Step(id="c", description="C", action="tool", tool_name="t3", depends_on=["a", "b"]),
    ]
    plan = Plan(steps=steps)

    call_order = []

    async def mock_execute(name, args):
        call_order.append(name)
        await asyncio.sleep(0.01)  # 模拟IO
        return f"result_{name}"

    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = mock_execute

    result = await execute_plan(plan, mock_executor)

    # a 和 b 应该在 c 之前完成
    assert call_order.index("t3") > call_order.index("t1")
    assert call_order.index("t3") > call_order.index("t2")
    assert result["a"] == "result_t1"
    assert result["b"] == "result_t2"
    assert result["c"] == "result_t3"


@pytest.mark.asyncio
async def test_execute_plan_with_concurrency_limit():
    """测试并行度限制"""
    steps = [
        Step(id="a", description="A", action="tool", tool_name="t1"),
        Step(id="b", description="B", action="tool", tool_name="t2"),
        Step(id="c", description="C", action="tool", tool_name="t3"),
    ]
    plan = Plan(steps=steps)

    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = ["r1", "r2", "r3"]

    result = await execute_plan(plan, mock_executor, max_concurrency=1)

    assert len(result) == 3
    assert mock_executor.execute.call_count == 3


@pytest.mark.asyncio
async def test_execute_step_timeout():
    """测试步骤执行超时"""
    step = Step(
        id="step1",
        description="慢操作",
        action="tool",
        tool_name="slow_tool"
    )

    async def slow_execute(name, args):
        await asyncio.sleep(10)
        return "结果"

    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = slow_execute

    with pytest.raises(StepExecutionError) as exc_info:
        await execute_step(step, {}, mock_executor, timeout=0.01)

    assert "超时" in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_step_no_timeout():
    """测试timeout=0不限制超时"""
    step = Step(
        id="step1",
        description="快速操作",
        action="tool",
        tool_name="fast_tool"
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = "结果"

    result = await execute_step(step, {}, mock_executor, timeout=0)
    assert result == "结果"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])