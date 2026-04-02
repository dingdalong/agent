import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass, field
from pydantic import BaseModel, ConfigDict

from src.graph.nodes import DecisionNode, SubgraphNode, TerminalNode
from src.graph.messages import AgentResponse, ResponseStatus


class DynamicState(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass
class MockContext:
    input: str = "test"
    state: DynamicState = field(default_factory=DynamicState)
    deps: MagicMock = field(default_factory=MagicMock)
    trace: list = field(default_factory=list)
    depth: int = 0


class TestDecisionNodeLLMFallback:
    """当 ui 为 None 时，DecisionNode 降级为 LLM 自动决策。"""

    def _make_ctx(self, llm_response_content: str) -> MockContext:
        mock_response = MagicMock()
        mock_response.content = llm_response_content
        mock_response.tool_calls = {}
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = mock_response
        ctx = MockContext()
        ctx.deps.llm = mock_llm
        ctx.deps.ui = None
        return ctx

    @pytest.mark.asyncio
    async def test_returns_chosen_branch(self):
        node = DecisionNode(name="decide", question="Is it ready?", branches=["yes", "no"])
        ctx = self._make_ctx("yes")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "yes"

    @pytest.mark.asyncio
    async def test_strips_whitespace(self):
        node = DecisionNode(name="d", question="?", branches=["yes", "no"])
        ctx = self._make_ctx("  no  \n")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "no"

    @pytest.mark.asyncio
    async def test_fuzzy_match_partial_label(self):
        """LLM 只返回 label 的一部分（如 'revise' 应匹配 'no, revise'）。"""
        node = DecisionNode(name="d", question="?", branches=["no, revise", "yes"])
        ctx = self._make_ctx("revise")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "no, revise"

    @pytest.mark.asyncio
    async def test_fuzzy_match_case_insensitive(self):
        node = DecisionNode(name="d", question="?", branches=["yes", "no"])
        ctx = self._make_ctx("YES")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "yes"

    @pytest.mark.asyncio
    async def test_fuzzy_match_quoted_response(self):
        node = DecisionNode(name="d", question="?", branches=["yes", "no"])
        ctx = self._make_ctx('"yes"')
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "yes"


class TestDecisionNodeUserInteraction:
    """当 ui 可用时，DecisionNode 向用户展示选项并等待选择。"""

    def _make_ctx(self, user_answer: str) -> MockContext:
        mock_ui = AsyncMock()
        mock_ui.prompt.return_value = user_answer
        ctx = MockContext()
        ctx.deps.ui = mock_ui
        return ctx

    @pytest.mark.asyncio
    async def test_user_selects_by_number(self):
        node = DecisionNode(name="d", question="Choose:", branches=["yes", "no"])
        ctx = self._make_ctx("1")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "yes"
        ctx.deps.ui.display.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_selects_by_text(self):
        node = DecisionNode(name="d", question="Choose:", branches=["yes", "no"])
        ctx = self._make_ctx("no")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "no"

    @pytest.mark.asyncio
    async def test_user_selects_by_number_second_option(self):
        node = DecisionNode(name="d", question="Choose:", branches=["approve", "revise"])
        ctx = self._make_ctx("2")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "revise"

    @pytest.mark.asyncio
    async def test_user_input_fuzzy_match(self):
        node = DecisionNode(name="d", question="?", branches=["no, revise", "yes, approve"])
        ctx = self._make_ctx("revise")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "no, revise"

    @pytest.mark.asyncio
    async def test_user_invalid_number_falls_back_to_match(self):
        node = DecisionNode(name="d", question="?", branches=["yes", "no"])
        ctx = self._make_ctx("9")
        result = await node.execute(ctx)
        # 编号 9 无效，_match_branch 回退返回 "9"
        assert result.output.data["chosen_branch"] == "9"

    @pytest.mark.asyncio
    async def test_empty_question_falls_back_to_name(self):
        """question 为空时，display 使用节点 name 作为问题文本。"""
        node = DecisionNode(
            name="Visual questions ahead?", question="", branches=["yes", "no"],
        )
        ctx = self._make_ctx("1")
        result = await node.execute(ctx)
        assert result.output.data["chosen_branch"] == "yes"
        displayed = ctx.deps.ui.display.call_args[0][0]
        assert "Visual questions ahead?" in displayed


class TestDecisionNodePersistsHistory:
    """DecisionNode 应将用户选择写入 conversation_history，供后续节点参考。"""

    def _make_ctx(self, user_answer: str) -> MockContext:
        mock_ui = AsyncMock()
        mock_ui.prompt.return_value = user_answer
        ctx = MockContext()
        ctx.deps.ui = mock_ui
        return ctx

    @pytest.mark.asyncio
    async def test_user_choice_persisted_to_conversation_history(self):
        """用户通过 UI 选择后，问题和回答应写入 conversation_history。"""
        node = DecisionNode(name="d", question="Choose plan:", branches=["yes", "no"])
        ctx = self._make_ctx("1")
        await node.execute(ctx)

        history = ctx.state.conversation_history
        assert history is not None
        assert len(history) == 2
        assert history[0]["role"] == "assistant"
        assert "Choose plan:" in history[0]["content"]
        assert history[1]["role"] == "user"
        assert history[1]["content"] == "yes"

    @pytest.mark.asyncio
    async def test_unmatched_choice_persisted_as_is(self):
        """用户输入不匹配任何分支时，原始输入仍应写入 history。"""
        node = DecisionNode(name="d", question="Pick:", branches=["yes", "no"])
        ctx = self._make_ctx("设计一：网格法")
        await node.execute(ctx)

        history = ctx.state.conversation_history
        assert history is not None
        assert history[1]["role"] == "user"
        assert history[1]["content"] == "设计一：网格法"

    @pytest.mark.asyncio
    async def test_appends_to_existing_history(self):
        """已有 conversation_history 时，应追加而非覆盖。"""
        node = DecisionNode(name="d", question="Ready?", branches=["yes", "no"])
        ctx = self._make_ctx("yes")
        ctx.state.conversation_history = [
            {"role": "assistant", "content": "previous"},
        ]
        await node.execute(ctx)

        history = ctx.state.conversation_history
        assert len(history) == 3  # 1 existing + 2 new
        assert history[0]["content"] == "previous"

    @pytest.mark.asyncio
    async def test_llm_fallback_also_persists_history(self):
        """无 UI 时 LLM 决策也应写入 conversation_history。"""
        mock_response = MagicMock()
        mock_response.content = "yes"
        mock_response.tool_calls = {}
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = mock_response
        ctx = MockContext()
        ctx.deps.llm = mock_llm
        ctx.deps.ui = None

        node = DecisionNode(name="d", question="Ready?", branches=["yes", "no"])
        await node.execute(ctx)

        history = ctx.state.conversation_history
        assert history is not None
        assert len(history) == 2
        assert history[0]["role"] == "assistant"
        assert history[1]["role"] == "user"
        assert history[1]["content"] == "yes"


class TestTerminalNode:
    @pytest.mark.asyncio
    async def test_passes_through_last_output(self):
        node = TerminalNode(name="end")
        ctx = MockContext()
        last = AgentResponse(text="done", data={"x": 1})
        ctx.state._last_output = last  # type: ignore[attr-defined]
        result = await node.execute(ctx)
        assert result.output is last

    @pytest.mark.asyncio
    async def test_empty_when_no_last_output(self):
        node = TerminalNode(name="end")
        ctx = MockContext()
        result = await node.execute(ctx)
        assert result.output.text == ""


class TestSubgraphNode:
    @pytest.mark.asyncio
    async def test_runs_sub_graph(self):
        from src.graph.engine import GraphResult

        mock_engine = AsyncMock()
        mock_engine.run.return_value = GraphResult(
            output=AgentResponse(text="sub result", data={"k": "v"}),
            state=DynamicState(),
        )

        sub_graph = MagicMock()
        node = SubgraphNode(name="sub", sub_graph=sub_graph)

        ctx = MockContext()
        ctx.deps.engine = mock_engine

        result = await node.execute(ctx)
        assert result.output.text == "sub result"
        mock_engine.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_depth_limit(self):
        node = SubgraphNode(name="sub", sub_graph=MagicMock(), max_subgraph_depth=2)
        ctx = MockContext(depth=2)
        ctx.deps.engine = AsyncMock()

        result = await node.execute(ctx)
        assert result.output.status == ResponseStatus.FAILED
        assert "深度超过限制" in result.output.text
        ctx.deps.engine.run.assert_not_called()
