"""解析 skill markdown 为 WorkflowPlan。

解析优先级：
1. 如果有 dot graph → 以 dot 为权威拓扑
2. checklist 作为补充 → 提供描述
3. 如果只有 checklist → 退化为线性序列
4. 两者都没有 → fallback 单步骤
"""
from __future__ import annotations

import re
import logging
from src.graph.workflow import StepType, WorkflowStep, WorkflowTransition, WorkflowPlan

logger = logging.getLogger(__name__)

# dot graph 中 shape 到 StepType 的映射
_SHAPE_MAP: dict[str, StepType] = {
    "box": StepType.ACTION,
    "diamond": StepType.DECISION,
    "doublecircle": StepType.TERMINAL,
}

# 匹配 checklist 条目：1. **Name** — description
_CHECKLIST_RE = re.compile(
    r"^\d+\.\s+\*\*(.+?)\*\*\s*(?:—|--|-)\s*(.+)$", re.MULTILINE
)

# dot 节点声明：  "Node Name" [shape=box];
_DOT_NODE_RE = re.compile(
    r'"([^"]+)"\s*\[([^\]]*)\]'
)

# dot 边声明：  "A" -> "B" [label="yes"];
_DOT_EDGE_RE = re.compile(
    r'"([^"]+)"\s*->\s*"([^"]+)"(?:\s*\[([^\]]*)\])?'
)

# label 提取
_LABEL_RE = re.compile(r'label\s*=\s*"([^"]*)"')

# shape 提取
_SHAPE_RE = re.compile(r'shape\s*=\s*(\w+)')

# "Invoke X skill" 模式
_INVOKE_SKILL_RE = re.compile(r"[Ii]nvoke\s+(\S+)\s+skill")


def _slugify(name: str) -> str:
    """将步骤名称转为 ID 安全的字符串。"""
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]", "_", name).strip("_").lower()


class SkillWorkflowParser:
    """解析 skill markdown 为 WorkflowPlan。"""

    def parse(self, content: str, skill_name: str) -> WorkflowPlan:
        dot_block = self._extract_dot(content)
        checklist = self._extract_checklist(content)
        sections = self._extract_sections(content)
        constraints = self._extract_constraints(content)

        if dot_block:
            return self._parse_dot(dot_block, checklist, sections, constraints, skill_name)
        elif checklist:
            return self._parse_checklist(checklist, sections, constraints, skill_name)
        else:
            return self._parse_fallback(content, skill_name)

    def _extract_dot(self, content: str) -> str | None:
        match = re.search(r"```dot\s*\n(.*?)```", content, re.DOTALL)
        return match.group(1) if match else None

    def _extract_checklist(self, content: str) -> list[tuple[str, str]]:
        return _CHECKLIST_RE.findall(content)

    def _extract_sections(self, content: str) -> dict[str, str]:
        """提取 **Name:** 或 ## Name 格式的 section 内容。"""
        result: dict[str, str] = {}
        # 匹配 **Name:** 段落
        bold_sections = re.findall(
            r"\*\*([^*]+?)(?:：|:)\*\*\s*\n(.*?)(?=\n\*\*[^*]+?(?:：|:)\*\*|\n##|\Z)",
            content,
            re.DOTALL,
        )
        for name, body in bold_sections:
            result[name.strip()] = body.strip()
        return result

    def _extract_constraints(self, content: str) -> list[str]:
        """提取 Key Principles / Anti-Pattern 等约束 section 的列表项。"""
        constraints: list[str] = []
        constraint_headers = ["Key Principles", "Anti-Pattern", "HARD-GATE", "Hard Gate", "Important"]
        for header in constraint_headers:
            pattern = rf"(?:##\s*{header}|<{header}>)(.*?)(?=\n##|\n<|\Z)"
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                items = re.findall(r"[-*]\s+(.+)", match.group(1))
                constraints.extend(items)
        return constraints

    def _parse_dot(
        self, dot: str, checklist: list[tuple[str, str]],
        sections: dict[str, str], constraints: list[str], skill_name: str,
    ) -> WorkflowPlan:
        checklist_map = {name.strip(): desc.strip() for name, desc in checklist}
        steps: list[WorkflowStep] = []
        transitions: list[WorkflowTransition] = []
        first_node: str | None = None

        # 解析节点
        for match in _DOT_NODE_RE.finditer(dot):
            name = match.group(1)
            attrs = match.group(2)
            shape_match = _SHAPE_RE.search(attrs)
            shape = shape_match.group(1) if shape_match else "box"
            step_type = _SHAPE_MAP.get(shape, StepType.ACTION)

            # 检测 subworkflow
            subworkflow_skill = None
            invoke_match = _INVOKE_SKILL_RE.search(name)
            if invoke_match:
                subworkflow_skill = invoke_match.group(1)
                step_type = StepType.SUBWORKFLOW

            step_id = _slugify(name)
            instructions = sections.get(name, checklist_map.get(name, ""))

            steps.append(WorkflowStep(
                id=step_id,
                name=name,
                instructions=instructions,
                step_type=step_type,
                subworkflow_skill=subworkflow_skill,
            ))
            if first_node is None:
                first_node = step_id

        # 解析边
        for match in _DOT_EDGE_RE.finditer(dot):
            from_name, to_name = match.group(1), match.group(2)
            attrs = match.group(3) or ""
            condition = None
            label_match = _LABEL_RE.search(attrs)
            if label_match:
                condition = label_match.group(1)

            transitions.append(WorkflowTransition(
                from_step=_slugify(from_name),
                to_step=_slugify(to_name),
                condition=condition,
            ))

        return WorkflowPlan(
            name=skill_name,
            steps=steps,
            transitions=transitions,
            entry_step=first_node or "main",
            constraints=constraints,
        )

    def _parse_checklist(
        self, checklist: list[tuple[str, str]],
        sections: dict[str, str], constraints: list[str], skill_name: str,
    ) -> WorkflowPlan:
        steps: list[WorkflowStep] = []
        transitions: list[WorkflowTransition] = []

        for i, (name, desc) in enumerate(checklist):
            name = name.strip()
            step_id = _slugify(name)
            instructions = sections.get(name, desc.strip())
            steps.append(WorkflowStep(
                id=step_id,
                name=name,
                instructions=instructions,
                step_type=StepType.ACTION,
            ))
            if i > 0:
                transitions.append(WorkflowTransition(
                    from_step=steps[i - 1].id,
                    to_step=step_id,
                ))

        return WorkflowPlan(
            name=skill_name,
            steps=steps,
            transitions=transitions,
            entry_step=steps[0].id if steps else "main",
            constraints=constraints,
        )

    def _parse_fallback(self, content: str, skill_name: str) -> WorkflowPlan:
        # 去掉 frontmatter
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[2] if len(parts) > 2 else content

        return WorkflowPlan(
            name=skill_name,
            steps=[WorkflowStep(
                id="main",
                name=skill_name,
                instructions=body.strip(),
                step_type=StepType.ACTION,
            )],
            transitions=[],
            entry_step="main",
            constraints=[],
        )
