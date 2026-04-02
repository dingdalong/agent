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

    def test_action_before_decision_gets_hint(self):
        """ACTION 后继为 DECISION 时，agent instructions 中注入决策提示。"""
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="explore", name="Explore", instructions="look around",
                             step_type=StepType.ACTION),
                WorkflowStep(id="decide", name="Ready?", instructions="",
                             step_type=StepType.DECISION),
            ],
            transitions=[
                WorkflowTransition(from_step="explore", to_step="decide"),
                WorkflowTransition(from_step="decide", to_step="explore", condition="no"),
            ],
            entry_step="explore",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        agent_node = graph.nodes["explore"]
        assert "严禁" in agent_node.agent.instructions
        assert "以陈述句结尾" in agent_node.agent.instructions

    def test_action_not_before_decision_no_hint(self):
        """ACTION 后继为非 DECISION 时，不注入决策提示。"""
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="s1", name="S1", instructions="do",
                             step_type=StepType.ACTION),
                WorkflowStep(id="s2", name="S2", instructions="more",
                             step_type=StepType.ACTION),
            ],
            transitions=[WorkflowTransition(from_step="s1", to_step="s2")],
            entry_step="s1",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        agent_node = graph.nodes["s1"]
        assert "不要向用户提问" not in agent_node.agent.instructions

    def test_decision_question_falls_back_to_step_name(self):
        """DECISION 节点 instructions 为空时，question 使用原始 step.name。"""
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="visual_questions_ahead_", name="Visual questions ahead?",
                             instructions="", step_type=StepType.DECISION),
                WorkflowStep(id="s1", name="S1", instructions="yes path",
                             step_type=StepType.ACTION),
            ],
            transitions=[
                WorkflowTransition(from_step="visual_questions_ahead_", to_step="s1",
                                   condition="yes"),
            ],
            entry_step="visual_questions_ahead_",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        decision_node = graph.nodes["visual_questions_ahead_"]
        assert decision_node.question == "Visual questions ahead?"

    def test_decision_question_uses_instructions_when_present(self):
        """DECISION 节点有 instructions 时，question 使用 instructions。"""
        plan = WorkflowPlan(
            name="test",
            steps=[
                WorkflowStep(id="d1", name="Ready?", instructions="Is everything ready?",
                             step_type=StepType.DECISION),
            ],
            transitions=[],
            entry_step="d1",
        )
        compiler = WorkflowCompiler()
        graph = compiler.compile(plan, agent_factory=make_agent)
        decision_node = graph.nodes["d1"]
        assert decision_node.question == "Is everything ready?"

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
