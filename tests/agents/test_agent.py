"""Agent / AgentResult / HandoffRequest 数据模型测试。"""
import pytest
from pydantic import BaseModel


class DummyOutput(BaseModel):
    score: float


def test_agent_basic_creation():
    from src.agents.agent import Agent

    agent = Agent(
        name="test",
        description="A test agent",
        instructions="You are a test agent.",
    )
    assert agent.name == "test"
    assert agent.description == "A test agent"
    assert agent.instructions == "You are a test agent."
    assert agent.tools == []
    assert agent.handoffs == []
    assert agent.output_model is None
    assert agent.input_guardrails == []
    assert agent.output_guardrails == []
    assert agent.hooks is None


def test_agent_with_all_fields():
    from src.agents.agent import Agent

    agent = Agent(
        name="weather",
        description="Weather agent",
        instructions="Check weather.",
        tools=["get_weather"],
        handoffs=["calendar_agent"],
        output_model=DummyOutput,
    )
    assert agent.tools == ["get_weather"]
    assert agent.handoffs == ["calendar_agent"]
    assert agent.output_model is DummyOutput


def test_agent_dynamic_instructions():
    from src.agents.agent import Agent

    def make_instructions(ctx):
        return f"Handle: {ctx.input}"

    agent = Agent(
        name="dynamic",
        description="Dynamic agent",
        instructions=make_instructions,
    )
    assert callable(agent.instructions)


def test_agent_result():
    from src.agents.agent import AgentResult
    from src.graph.messages import AgentResponse

    result = AgentResult(response=AgentResponse(text="hello"))
    assert result.text == "hello"
    assert result.data == {}
    assert result.handoff is None


def test_agent_result_with_handoff():
    from src.agents.agent import AgentResult, HandoffRequest
    from src.graph.messages import AgentMessage, AgentResponse

    msg = AgentMessage(objective="book meeting", task="book meeting")
    handoff = HandoffRequest(target="calendar", message=msg)
    result = AgentResult(
        response=AgentResponse(text="transferring"),
        handoff=handoff,
    )
    assert result.handoff.target == "calendar"
    assert result.handoff.message.task == "book meeting"


def test_handoff_request():
    from src.agents.agent import HandoffRequest
    from src.graph.messages import AgentMessage

    msg = AgentMessage(objective="send report", task="send report")
    req = HandoffRequest(target="email_agent", message=msg)
    assert req.target == "email_agent"
    assert req.message.task == "send report"
