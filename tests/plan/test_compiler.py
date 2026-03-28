"""测试 plan.compiler 模块"""
import pytest
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from src.plan.models import Step, Plan
from src.plan.compiler import PlanCompiler, resolve_variables
from src.plan.exceptions import CompileError
from src.agents import AgentRegistry, Agent, RunContext, DynamicState
from src.graph import GraphEngine, NodeResult, FunctionNode


# === resolve_variables 单元测试 ===

class TestResolveVariables:
    def test_simple_reference(self):
        ctx = {"search": "sunny 25°C"}
        assert resolve_variables("$search", ctx) == "sunny 25°C"

    def test_nested_reference(self):
        ctx = {"search": {"results": ["a", "b"]}}
        assert resolve_variables("$search.results", ctx) == ["a", "b"]

    def test_deep_nested(self):
        ctx = {"api": {"data": {"name": "test"}}}
        assert resolve_variables("$api.data.name", ctx) == "test"

    def test_dict_args(self):
        ctx = {"s1": "hello"}
        result = resolve_variables({"text": "$s1", "lang": "zh"}, ctx)
        assert result == {"text": "hello", "lang": "zh"}

    def test_list_args(self):
        ctx = {"s1": "a", "s2": "b"}
        result = resolve_variables(["$s1", "$s2", "literal"], ctx)
        assert result == ["a", "b", "literal"]

    def test_missing_reference_unchanged(self):
        ctx = {}
        assert resolve_variables("$missing", ctx) == "$missing"

    def test_non_variable_unchanged(self):
        ctx = {"s1": "val"}
        assert resolve_variables("plain text", ctx) == "plain text"
        assert resolve_variables(42, ctx) == 42
        assert resolve_variables(None, ctx) is None


# === PlanCompiler 单元测试 ===

def _make_registry_and_router():
    """创建测试用的 registry 和 router mock"""
    registry = AgentRegistry()
    registry.register(Agent(
        name="helper",
        description="帮助 agent",
        instructions="你是助手",
    ))

    router = MagicMock()
    router.get_all_schemas.return_value = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取天气",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    return registry, router


class TestPlanCompilerValidation:
    def test_empty_plan_raises(self):
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        with pytest.raises(CompileError, match="空计划"):
            compiler.compile(Plan(steps=[]))

    def test_duplicate_step_ids_raises(self):
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="s1", description="a", tool_name="get_weather"),
            Step(id="s1", description="b", tool_name="get_weather"),
        ])
        with pytest.raises(CompileError, match="重复"):
            compiler.compile(plan)

    def test_missing_dependency_raises(self):
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="s1", description="a", tool_name="get_weather", depends_on=["nonexistent"]),
        ])
        with pytest.raises(CompileError, match="不存在"):
            compiler.compile(plan)

    def test_cycle_raises(self):
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="s1", description="a", tool_name="get_weather", depends_on=["s2"]),
            Step(id="s2", description="b", tool_name="get_weather", depends_on=["s1"]),
        ])
        with pytest.raises(CompileError, match="循环"):
            compiler.compile(plan)

    def test_unknown_agent_raises(self):
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="s1", description="a", agent_name="nonexistent"),
        ])
        with pytest.raises(CompileError, match="Agent.*不存在"):
            compiler.compile(plan)


class TestPlanCompilerCompile:
    def test_single_tool_step(self):
        """单步工具计划 -> 单节点图"""
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="weather", description="查询天气", tool_name="get_weather"),
        ])
        graph = compiler.compile(plan)
        assert graph.entry == "weather"
        assert "weather" in graph.nodes
        assert isinstance(graph.nodes["weather"], FunctionNode)

    def test_sequential_steps(self):
        """顺序依赖 -> 顺序边"""
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="s1", description="第一步", tool_name="get_weather"),
            Step(id="s2", description="第二步", tool_name="get_weather", depends_on=["s1"]),
        ])
        graph = compiler.compile(plan)
        assert graph.entry == "s1"
        edge_pairs = [(e.source, e.target) for e in graph.edges]
        assert ("s1", "s2") in edge_pairs

    def test_parallel_steps(self):
        """无依赖的同层步骤 -> ParallelGroup"""
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="a", description="A", tool_name="get_weather"),
            Step(id="b", description="B", tool_name="get_weather"),
            Step(id="c", description="汇总", tool_name="get_weather", depends_on=["a", "b"]),
        ])
        graph = compiler.compile(plan)
        assert graph.entry == "a"
        assert len(graph.parallel_groups) == 1
        pg = graph.parallel_groups[0]
        assert set(pg.nodes) == {"a", "b"}

    def test_agent_step(self):
        """Agent 步骤 -> FunctionNode（内部调用 AgentRunner）"""
        registry, router = _make_registry_and_router()
        compiler = PlanCompiler(registry, router)
        plan = Plan(steps=[
            Step(id="draft", description="起草", agent_name="helper"),
        ])
        graph = compiler.compile(plan)
        assert "draft" in graph.nodes
        assert isinstance(graph.nodes["draft"], FunctionNode)


@pytest.mark.asyncio
class TestPlanCompilerExecution:
    async def test_tool_step_calls_router(self):
        """编译后的工具步骤执行时调用 tool_router.route"""
        registry, router = _make_registry_and_router()
        router.route = AsyncMock(return_value="晴天 25°C")
        compiler = PlanCompiler(registry, router)

        plan = Plan(steps=[
            Step(id="weather", description="查天气", tool_name="get_weather", tool_args={"location": "广州"}),
        ])
        graph = compiler.compile(plan)
        engine = GraphEngine()

        from pydantic import ConfigDict
        from pydantic import BaseModel

        class TestDeps(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            tool_router: object = None

        ctx = RunContext(input="test", state=DynamicState(), deps=TestDeps(tool_router=router))
        result = await engine.run(graph, ctx)

        router.route.assert_called_once_with("get_weather", {"location": "广州"})
        assert result.output == "晴天 25°C"

    async def test_variable_resolution_during_execution(self):
        """执行时 $step_id.field 变量正确解析"""
        registry, router = _make_registry_and_router()

        call_count = 0
        async def mock_route(tool_name, args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "sunny"
            return f"translated: {args.get('text', '')}"

        router.route = mock_route
        compiler = PlanCompiler(registry, router)

        plan = Plan(steps=[
            Step(id="search", description="搜索", tool_name="get_weather"),
            Step(
                id="translate",
                description="翻译",
                tool_name="get_weather",
                tool_args={"text": "$search"},
                depends_on=["search"],
            ),
        ])
        graph = compiler.compile(plan)
        engine = GraphEngine()

        from pydantic import ConfigDict, BaseModel

        class TestDeps(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            tool_router: object = None

        ctx = RunContext(input="test", state=DynamicState(), deps=TestDeps(tool_router=router))
        result = await engine.run(graph, ctx)

        assert "sunny" in str(result.output)
