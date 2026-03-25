"""多智能体协作包。"""

from src.agents.registry import AgentDef, AgentRegistry
from src.agents.orchestrator import MultiAgentFlow
from src.agents.specialists import setup_agents

agent_registry = AgentRegistry()
setup_agents(agent_registry)

__all__ = ["agent_registry", "MultiAgentFlow", "AgentRegistry", "AgentDef"]
