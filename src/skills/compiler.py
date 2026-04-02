"""将 WorkflowPlan 编译为 CompiledGraph。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from src.agents.node import AgentNode
from src.graph.builder import GraphBuilder
from src.graph.nodes import DecisionNode, SubgraphNode, TerminalNode
from src.graph.types import CompiledGraph
from src.graph.workflow import StepType, WorkflowPlan

if TYPE_CHECKING:
    from src.agents.agent import Agent
    from src.skills.manager import SkillManager

logger = logging.getLogger(__name__)


class WorkflowCompiler:
    """将 WorkflowPlan 编译为可执行的 CompiledGraph。"""

    def compile(
        self,
        plan: WorkflowPlan,
        agent_factory: Callable[[str, str], Agent],
        skill_manager: SkillManager | None = None,
    ) -> CompiledGraph:
        builder = GraphBuilder()

        # 将约束拼接为前缀，注入每个 ACTION 步骤的 instructions
        constraint_prefix = ""
        if plan.constraints:
            lines = "\n".join(f"- {c}" for c in plan.constraints)
            constraint_prefix = f"## 约束\n{lines}\n\n"

        # 预建 step_id → step 映射，用于查找后继节点类型
        step_map = {s.id: s for s in plan.steps}

        for step in plan.steps:
            match step.step_type:
                case StepType.ACTION:
                    instructions = constraint_prefix + step.instructions
                    # 如果后继是 DECISION 节点，注入约束：不要自行生成选项
                    decision_hint = self._build_decision_hint(
                        step.id, plan, step_map,
                    )
                    if decision_hint:
                        instructions += decision_hint
                    agent = agent_factory(step.id, instructions)
                    node = AgentNode(agent)
                    # 确保节点名称与 step.id 一致，不依赖 agent.name
                    node.name = step.id
                    builder.add_node(node)

                case StepType.DECISION:
                    branches = [
                        t.condition
                        for t in plan.transitions
                        if t.from_step == step.id and t.condition
                    ]
                    node = DecisionNode(
                        name=step.id,
                        question=step.instructions or step.name,
                        branches=branches,
                    )
                    builder.add_node(node)

                case StepType.SUBWORKFLOW:
                    sub_graph = self._compile_subworkflow(
                        step.subworkflow_skill or "",
                        skill_manager,
                        agent_factory,
                    )
                    builder.add_node(SubgraphNode(
                        name=step.id, sub_graph=sub_graph,
                    ))

                case StepType.TERMINAL:
                    builder.add_node(TerminalNode(name=step.id))

        for t in plan.transitions:
            builder.add_edge(t.from_step, t.to_step, condition=t.condition)

        builder.set_entry(plan.entry_step)
        return builder.compile()

    @staticmethod
    def _build_decision_hint(
        step_id: str,
        plan: WorkflowPlan,
        step_map: dict[str, Any],
    ) -> str:
        """若 ACTION 的后继是 DECISION，返回约束提示；否则返回空串。"""
        for t in plan.transitions:
            if t.from_step == step_id:
                successor = step_map.get(t.to_step)
                if successor and successor.step_type == StepType.DECISION:
                    question = successor.instructions or successor.name
                    branches = [
                        tr.condition
                        for tr in plan.transitions
                        if tr.from_step == successor.id and tr.condition
                    ]
                    branch_text = "、".join(branches) if branches else ""
                    return (
                        f"\n\n## ⚠️ 输出格式约束（必须遵守）\n"
                        f"你的回复**必须以陈述句结尾**，严禁以问句结尾。\n"
                        f"**严禁**向用户提问、征求意见或列出选项供用户选择。\n"
                        f"完成任务后直接给出结论即可。\n"
                        f"系统会在你回复后自动询问用户「{question}」"
                        f"（{branch_text}），无需你代劳。"
                    )
        return ""

    def _compile_subworkflow(
        self,
        skill_name: str,
        skill_manager: SkillManager | None,
        agent_factory: Callable[[str, str], Agent],
    ) -> CompiledGraph:
        if skill_manager is None:
            raise ValueError(
                f"需要 skill_manager 来编译子工作流 '{skill_name}'"
            )

        from src.skills.workflow_parser import SkillWorkflowParser

        content = skill_manager.activate(skill_name)
        parser = SkillWorkflowParser()
        sub_plan = parser.parse(content, skill_name)
        return self.compile(sub_plan, agent_factory, skill_manager)
