"""附加图节点类型：决策、子图、终止。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.graph.messages import AgentResponse, ResponseStatus
from src.graph.types import NodeResult


@dataclass
class DecisionNode:
    """LLM 评估条件，选择分支。输出 chosen_branch 供条件边匹配。"""
    name: str
    question: str
    branches: list[str]

    async def execute(self, context: Any) -> NodeResult:
        options_lines = "\n".join(
            f"  {i + 1}. {branch}" for i, branch in enumerate(self.branches)
        )
        # question 为空时 fallback 到节点名称
        display_question = self.question or self.name

        ui = getattr(context.deps, "ui", None)
        if ui is not None:
            # 用户决策模式：展示选项，等待用户选择
            await ui.display(f"\n{display_question}\n{options_lines}")
            answer = (await ui.prompt("请选择 (输入编号或选项文本): ")).strip()
            # 支持编号选择
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(self.branches):
                    matched = self.branches[idx]
                else:
                    matched = self._match_branch(answer)
            else:
                matched = self._match_branch(answer)
        else:
            # 无 UI 时降级为 LLM 自动决策
            prompt = (
                f"Based on the current state, choose the next action.\n\n"
                f"Current state: {context.input}\n"
                f"Options:\n{options_lines}\n\n"
                f"Reply with ONLY the exact option text (not the number)."
            )
            messages = [{"role": "user", "content": prompt}]
            response = await context.deps.llm.chat(messages)
            choice = response.content.strip().strip('"').strip("'")
            matched = self._match_branch(choice)

        # 将决策问答写入 conversation_history，供后续节点参考
        self._persist_decision(context, display_question, options_lines, matched)

        return NodeResult(
            output=AgentResponse(
                text=matched,
                data={"chosen_branch": matched},
            ),
        )

    @staticmethod
    def _persist_decision(
        context: Any, question: str, options_lines: str, matched: str,
    ) -> None:
        """将决策的问题和用户选择追加到 conversation_history。"""
        from src.agents.context import AppState

        new_turns = [
            {"role": "assistant", "content": f"{question}\n{options_lines}"},
            {"role": "user", "content": matched},
        ]

        if isinstance(context.state, AppState):
            if context.state.conversation_history is None:
                context.state.conversation_history = []
            context.state.conversation_history.extend(new_turns)
        else:
            history = getattr(context.state, "conversation_history", None)
            if history is None:
                history = []
                try:
                    setattr(context.state, "conversation_history", history)
                except (AttributeError, ValueError):
                    return
            history.extend(new_turns)

    def _match_branch(self, choice: str) -> str:
        """将 LLM 回复匹配到最佳 branch label。"""
        lower = choice.lower()
        # 精确匹配
        for branch in self.branches:
            if branch.lower() == lower:
                return branch
        # choice 是 branch 的子串（如 "revise" 匹配 "no, revise"）
        for branch in self.branches:
            if lower in branch.lower():
                return branch
        # branch 是 choice 的子串
        for branch in self.branches:
            if branch.lower() in lower:
                return branch
        # 无匹配，返回原始 choice，让 _resolve_edges 处理 fallback
        return choice


@dataclass
class SubgraphNode:
    """嵌套执行另一个编译好的图。"""
    name: str
    sub_graph: Any  # CompiledGraph — 避免循环导入
    max_subgraph_depth: int = 3

    async def execute(self, context: Any) -> NodeResult:
        from src.agents.context import DynamicState, RunContext

        current_depth = getattr(context, "depth", 0)
        if current_depth >= self.max_subgraph_depth:
            return NodeResult(
                output=AgentResponse(
                    text=f"错误：子图嵌套深度超过限制 ({self.max_subgraph_depth})",
                    status=ResponseStatus.FAILED,
                ),
            )

        sub_ctx = RunContext(
            input=context.input,
            state=DynamicState(),
            deps=context.deps,
            depth=current_depth + 1,
        )
        result = await context.deps.engine.run(self.sub_graph, sub_ctx)
        return NodeResult(output=AgentResponse.from_graph_result(result))


@dataclass
class TerminalNode:
    """工作流终止节点，透传上一步的输出。"""
    name: str

    async def execute(self, context: Any) -> NodeResult:
        last = getattr(context.state, "_last_output", None)
        if last is None:
            last = AgentResponse(text="", data={})
        return NodeResult(output=last)
