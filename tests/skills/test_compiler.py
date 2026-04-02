# tests/skills/test_compiler.py
"""WorkflowCompiler 测试 — 将 WorkflowPlan 编译为 CompiledGraph。"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from src.graph.workflow import StepType, WorkflowStep, WorkflowTransition, WorkflowPlan
from src.skills.compiler import WorkflowCompiler
from src.graph.nodes import DecisionNode, SubgraphNode, TerminalNode
from src.agents.node import AgentNode


def make_agent(name: str, instructions: str):
    """创建简单 Agent 用于测试（name == step_id）。"""
    from src.agents.agent import Agent
    return Agent(name=name, description="test", instructions=instructions)


def make_prefixed_agent(step_id: str, instructions: str):
    """模拟 app.py 的真实 factory：agent.name != step_id。"""
    from src.agents.agent import Agent
    return Agent(name=f"step_{step_id}", description="test", instructions=instructions)


class TestWorkflowCompiler:
    def test_action_step_becomes_agent_node(self):
        plan = WorkflowPlan(
            name="test",
            steps=[WorkflowStep(id="s1", name="S1", instructions="do it",
                                step_type=StepType.ACTION)],
            transitions=[],
            entry_step="s1",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        assert "s1" in graph.nodes
        assert isinstance(graph.nodes["s1"], AgentNode)

    def test_decision_step_becomes_decision_node(self):
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="d1", name="Ready?", instructions="check",
                             step_type=StepType.DECISION),
                WorkflowStep(id="s1", name="S1", instructions="yes path",
                             step_type=StepType.ACTION),
                WorkflowStep(id="s2", name="S2", instructions="no path",
                             step_type=StepType.ACTION),
            ],
            transitions=[
                WorkflowTransition(from_step="d1", to_step="s1", condition="yes"),
                WorkflowTransition(from_step="d1", to_step="s2", condition="no"),
            ],
            entry_step="d1",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        assert isinstance(graph.nodes["d1"], DecisionNode)
        assert len(graph.edges) == 2

    def test_terminal_step_becomes_terminal_node(self):
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="s1", name="S1", instructions="do",
                             step_type=StepType.ACTION),
                WorkflowStep(id="end", name="End", instructions="",
                             step_type=StepType.TERMINAL),
            ],
            transitions=[WorkflowTransition(from_step="s1", to_step="end")],
            entry_step="s1",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        assert isinstance(graph.nodes["end"], TerminalNode)

    def test_subworkflow_step(self):
        plan = WorkflowPlan(
            name="test",
            steps=[WorkflowStep(
                id="sub", name="Invoke foo",
                instructions="", step_type=StepType.SUBWORKFLOW,
                subworkflow_skill="foo",
            )],
            transitions=[],
            entry_step="sub",
        )
        mock_manager = MagicMock()
        mock_manager.activate.return_value = "# Foo\n\nJust do foo."

        compiler = WorkflowCompiler()
        graph = compiler.compile(
            plan, agent_factory=make_agent, skill_manager=mock_manager,
        )
        assert isinstance(graph.nodes["sub"], SubgraphNode)

    def test_entry_set_correctly(self):
        plan = WorkflowPlan(
            name="test",
            steps=[WorkflowStep(id="start", name="Start", instructions="go",
                                step_type=StepType.ACTION)],
            transitions=[],
            entry_step="start",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        assert graph.entry == "start"

    def test_constraints_injected_into_instructions(self):
        plan = WorkflowPlan(
            name="test",
            steps=[WorkflowStep(id="s1", name="S1", instructions="do it",
                                step_type=StepType.ACTION)],
            transitions=[],
            entry_step="s1",
            constraints=["Always be careful"],
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        agent_node = graph.nodes["s1"]
        # agent 的 instructions 应该包含约束
        assert "Always be careful" in agent_node.agent.instructions

    def test_agent_name_differs_from_step_id(self):
        """agent.name 与 step.id 不同时，节点仍以 step.id 注册。"""
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="main", name="Main", instructions="do",
                             step_type=StepType.ACTION),
                WorkflowStep(id="end", name="End", instructions="",
                             step_type=StepType.TERMINAL),
            ],
            transitions=[WorkflowTransition(from_step="main", to_step="end")],
            entry_step="main",
        )
        compiler = WorkflowCompiler()
        # make_prefixed_agent 产生 agent.name="step_main"，不等于 step.id="main"
        graph = compiler.compile(plan, agent_factory=make_prefixed_agent)
        assert "main" in graph.nodes
        assert graph.entry == "main"
