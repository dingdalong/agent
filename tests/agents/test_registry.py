"""AgentRegistry 测试。"""
import pytest
from src.agents.agent import Agent


@pytest.fixture
def registry():
    from src.agents.registry import AgentRegistry
    return AgentRegistry()


@pytest.fixture
def weather_agent():
    return Agent(
        name="weather_agent",
        description="查询天气",
        instructions="你是天气查询专家。",
        tools=["get_weather"],
    )


@pytest.fixture
def calendar_agent():
    return Agent(
        name="calendar_agent",
        description="管理日历",
        instructions="你是日历管理专家。",
        tools=["create_event"],
    )


def test_register_and_get(registry, weather_agent):
    registry.register(weather_agent)
    assert registry.get("weather_agent") is weather_agent


def test_get_nonexistent_returns_none(registry):
    assert registry.get("nonexistent") is None


def test_all_agents(registry, weather_agent, calendar_agent):
    registry.register(weather_agent)
    registry.register(calendar_agent)
    agents = registry.all_agents()
    assert len(agents) == 2
    names = {a.name for a in agents}
    assert names == {"weather_agent", "calendar_agent"}


def test_register_overwrite(registry, weather_agent):
    registry.register(weather_agent)
    updated = Agent(
        name="weather_agent",
        description="Updated",
        instructions="Updated instructions.",
    )
    registry.register(updated)
    assert registry.get("weather_agent").description == "Updated"


def test_has(registry, weather_agent):
    assert not registry.has("weather_agent")
    registry.register(weather_agent)
    assert registry.has("weather_agent")
