# tests/skills/test_workflow_parser.py
"""SkillWorkflowParser 测试 — 解析 skill markdown 为 WorkflowPlan。"""
import pytest
from src.skills.workflow_parser import SkillWorkflowParser
from src.graph.workflow import StepType


SAMPLE_SKILL_WITH_DOT = '''
# Test Skill

## Checklist

1. **Step one** — do first thing
2. **Step two** — do second thing
3. **Done** — finish

## Process Flow

```dot
digraph test {
    "Step one" [shape=box];
    "Ready?" [shape=diamond];
    "Step two" [shape=box];
    "Done" [shape=doublecircle];

    "Step one" -> "Ready?";
    "Ready?" -> "Step two" [label="yes"];
    "Ready?" -> "Step one" [label="no"];
    "Step two" -> "Done";
}
```

## The Process

**Step one:**
Do the first thing carefully.

**Step two:**
Do the second thing.

## Key Principles
- Always be careful
- Never assume
'''

SAMPLE_SKILL_CHECKLIST_ONLY = '''
# Simple Skill

## Checklist

1. **Alpha** — first
2. **Beta** — second
3. **Gamma** — third
'''

SAMPLE_SKILL_NO_STRUCTURE = '''
# Plain Skill

Just do whatever this says.
Follow these instructions.
'''

SAMPLE_SKILL_WITH_SUBWORKFLOW = '''
# Skill With Sub

## Checklist

1. **Do work** — work
2. **Invoke writing-plans skill** — transition

## Process Flow

```dot
digraph sub {
    "Do work" [shape=box];
    "Invoke writing-plans skill" [shape=doublecircle];
    "Do work" -> "Invoke writing-plans skill";
}
```
'''


class TestDotGraphParsing:
    def test_extracts_nodes_and_edges(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")

        names = {s.name for s in plan.steps}
        assert "Step one" in names
        assert "Ready?" in names
        assert "Step two" in names
        assert "Done" in names

    def test_node_types_from_shapes(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")

        by_name = {s.name: s for s in plan.steps}
        assert by_name["Step one"].step_type == StepType.ACTION
        assert by_name["Ready?"].step_type == StepType.DECISION
        assert by_name["Done"].step_type == StepType.TERMINAL

    def test_transitions_with_conditions(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")

        conditionals = [t for t in plan.transitions if t.condition]
        assert len(conditionals) == 2
        conditions = {t.condition for t in conditionals}
        assert conditions == {"yes", "no"}

    def test_entry_step(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")
        # 入口是 dot graph 中第一个被声明的节点
        assert plan.entry_step is not None

    def test_constraints_extracted(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")
        assert len(plan.constraints) >= 1
        assert any("careful" in c for c in plan.constraints)

    def test_instructions_from_sections(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_DOT, "test-skill")
        by_name = {s.name: s for s in plan.steps}
        assert "first thing carefully" in by_name["Step one"].instructions


class TestChecklistOnlyParsing:
    def test_linear_sequence(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_CHECKLIST_ONLY, "simple")

        assert len(plan.steps) == 3
        assert all(s.step_type == StepType.ACTION for s in plan.steps)

        # 线性转换：Alpha → Beta → Gamma
        assert len(plan.transitions) == 2
        assert plan.transitions[0].from_step == plan.steps[0].id
        assert plan.transitions[0].to_step == plan.steps[1].id


class TestFallbackParsing:
    def test_no_structure_becomes_single_action(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_NO_STRUCTURE, "plain")

        assert len(plan.steps) == 1
        assert plan.steps[0].step_type == StepType.ACTION
        assert "Just do whatever" in plan.steps[0].instructions


class TestSubworkflowDetection:
    def test_invoke_skill_detected(self):
        parser = SkillWorkflowParser()
        plan = parser.parse(SAMPLE_SKILL_WITH_SUBWORKFLOW, "sub-test")

        by_name = {s.name: s for s in plan.steps}
        invoke_step = by_name["Invoke writing-plans skill"]
        assert invoke_step.step_type == StepType.SUBWORKFLOW
        assert invoke_step.subworkflow_skill == "writing-plans"
