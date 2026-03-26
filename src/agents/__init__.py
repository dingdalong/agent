"""Agents 模块 — 图引擎 + Agent Runner 混合架构。

对外导出所有公共接口。
"""

from src.agents.agent import Agent, AgentResult, HandoffRequest
from src.agents.registry import AgentRegistry
from src.agents.runner import AgentRunner
from src.agents.context import RunContext, TraceEvent, DictState, EmptyDeps
from src.agents.guardrails import Guardrail, GuardrailResult, run_guardrails
from src.agents.hooks import AgentHooks, GraphHooks
from src.agents.graph.types import (
    GraphNode,
    AgentNode,
    FunctionNode,
    NodeResult,
    Edge,
    CompiledGraph,
    ParallelGroup,
)
from src.agents.graph.builder import GraphBuilder
from src.agents.graph.engine import GraphEngine, GraphResult

__all__ = [
    # Agent
    "Agent",
    "AgentResult",
    "HandoffRequest",
    # Registry
    "AgentRegistry",
    # Runner
    "AgentRunner",
    # Context
    "RunContext",
    "TraceEvent",
    "DictState",
    "EmptyDeps",
    # Guardrails
    "Guardrail",
    "GuardrailResult",
    "run_guardrails",
    # Hooks
    "AgentHooks",
    "GraphHooks",
    # Graph types
    "GraphNode",
    "AgentNode",
    "FunctionNode",
    "NodeResult",
    "Edge",
    "CompiledGraph",
    "ParallelGroup",
    # Graph builder & engine
    "GraphBuilder",
    "GraphEngine",
    "GraphResult",
]
