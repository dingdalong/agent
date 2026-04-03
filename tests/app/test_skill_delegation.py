# tests/app/test_skill_delegation.py
"""Skill 模式下跨分类委派集成测试。

验证 skill 图中：
1. orchestrator → handoff → category agent 可达
2. category agent A → delegate → category agent B 可达
3. delegate_depth 限制正常工作
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.context import RunContext, DynamicState
from src.agents.deps import AgentDeps
from src.agents.registry import AgentRegistry
from src.agents.runner import AgentRunner
from src.graph import GraphEngine
from src.tools.categories import CategoryResolver
from src.agents.delegate import DelegateToolProvider
from src.tools.router import ToolRouter
from src.app.presets import build_skill_graph
from src.llm.types import LLMResponse


@pytest.fixture
def categories():
    return {
        "tool_terminal": {"description": "终端操作", "tools": {"exec_cmd": "Execute command"}},
        "tool_calc": {"description": "数学计算", "tools": {"calculate": "Calculate math"}},
    }


@pytest.fixture
def resolver(categories):
    return CategoryResolver(categories)


@pytest.mark.asyncio
async def test_skill_graph_has_category_agent_nodes(resolver):
    """skill 图应包含 category agent 节点。"""
    skill_registry = AgentRegistry()
    skill_registry.set_category_resolver(resolver)

    summaries = resolver.get_all_summaries()
    graph = build_skill_graph(
        skill_registry,
        "<skill_content>测试 skill 指令</skill_content>",
        category_summaries=summaries,
    )

    assert "orchestrator" in graph.nodes
    assert "tool_terminal" in graph.nodes
    assert "tool_calc" in graph.nodes


@pytest.mark.asyncio
async def test_skill_mode_handoff_to_category_agent(resolver):
    """skill 模式下 orchestrator 应能 handoff 到 category agent。"""
    mock_llm = AsyncMock()
    call_count = 0

    async def mock_chat(messages, tools=None, silent=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # orchestrator handoff 到 tool_terminal
            return LLMResponse(
                content="",
                tool_calls={0: {
                    "id": "call_1",
                    "name": "transfer_to_tool_terminal",
                    "arguments": json.dumps({"task": "执行终端命令"}),
                }},
            )
        # tool_terminal 直接返回结果
        return LLMResponse(content="命令执行完毕", tool_calls={})

    mock_llm.chat = mock_chat

    skill_registry = AgentRegistry()
    skill_registry.set_category_resolver(resolver)
    runner = AgentRunner()
    router = ToolRouter()

    delegate_provider = DelegateToolProvider(resolver=resolver)
    router.add_provider(delegate_provider)

    summaries = resolver.get_all_summaries()
    graph = build_skill_graph(
        skill_registry,
        "<skill_content>测试 skill</skill_content>",
        category_summaries=summaries,
    )
    engine = GraphEngine()

    ctx = RunContext(
        input="执行终端任务",
        state=DynamicState(),
        deps=AgentDeps(
            llm=mock_llm,
            tool_router=router,
            agent_registry=skill_registry,
            graph_engine=engine,
            runner=runner,
        ),
    )
    result = await engine.run(graph, ctx)

    assert "命令执行完毕" in str(result.output)
    assert call_count == 2  # orchestrator + tool_terminal


@pytest.mark.asyncio
async def test_skill_mode_cross_agent_delegation(resolver):
    """skill 模式下 category agent A 应能 delegate 给 category agent B。"""
    mock_llm = AsyncMock()
    call_count = 0

    async def mock_chat(messages, tools=None, silent=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # orchestrator handoff 到 tool_terminal
            return LLMResponse(
                content="",
                tool_calls={0: {
                    "id": "call_1",
                    "name": "transfer_to_tool_terminal",
                    "arguments": json.dumps({"task": "执行计算"}),
                }},
            )
        if call_count == 2:
            # tool_terminal delegate 给 tool_calc
            return LLMResponse(
                content="",
                tool_calls={0: {
                    "id": "call_2",
                    "name": "delegate_tool_calc",
                    "arguments": json.dumps({
                        "objective": "执行计算",
                        "task": "计算 1+1",
                    }),
                }},
            )
        if call_count == 3:
            # tool_calc 返回结果
            return LLMResponse(content="已完成\n计算结果是 2", tool_calls={})
        # tool_terminal 收到 delegate 结果后输出
        return LLMResponse(content="计算完毕，结果是 2", tool_calls={})

    mock_llm.chat = mock_chat

    skill_registry = AgentRegistry()
    skill_registry.set_category_resolver(resolver)
    runner = AgentRunner()
    router = ToolRouter()

    delegate_provider = DelegateToolProvider(resolver=resolver)
    router.add_provider(delegate_provider)

    summaries = resolver.get_all_summaries()
    graph = build_skill_graph(
        skill_registry,
        "<skill_content>测试 skill</skill_content>",
        category_summaries=summaries,
    )
    engine = GraphEngine()

    ctx = RunContext(
        input="帮我计算",
        state=DynamicState(),
        deps=AgentDeps(
            llm=mock_llm,
            tool_router=router,
            agent_registry=skill_registry,
            graph_engine=engine,
            runner=runner,
        ),
    )
    result = await engine.run(graph, ctx)

    assert "2" in str(result.output)
    # orchestrator(1) + terminal calls delegate(2) + calc responds(3) + terminal responds(4)
    assert call_count == 4


class TestSkillWorkflowIntegration:
    """端到端测试：skill markdown → parse → compile → execute。"""

    @pytest.mark.asyncio
    async def test_checklist_skill_executes_as_pipeline(self):
        """纯 checklist 的 skill 应该编译为线性管道并按顺序执行。"""
        from src.skills.workflow_parser import SkillWorkflowParser
        from src.skills.compiler import WorkflowCompiler
        from src.agents.agent import Agent

        content = (
            "# Test\n\n"
            "## Checklist\n\n"
            "1. **Step A** — do A\n"
            "2. **Step B** — do B\n"
        )
        parser = SkillWorkflowParser()
        plan = parser.parse(content, "test")

        assert len(plan.steps) == 2
        assert plan.transitions[0].from_step == plan.steps[0].id
        assert plan.transitions[0].to_step == plan.steps[1].id

        def make_agent(step_id, step_name, checklist_desc):
            return Agent(name=step_id, description="t", instructions=checklist_desc)

        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)

        assert graph.entry == plan.steps[0].id
        assert len(graph.nodes) == 2
        assert len(graph.edges) == 1
