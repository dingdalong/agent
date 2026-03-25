"""AgentDef + AgentRegistry：专业 Agent 的定义与注册表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Type

from pydantic import BaseModel


@dataclass
class AgentDef:
    """专业 Agent 定义。"""

    name: str
    description: str
    tool_names: list[str]
    system_prompt: str
    output_model: Optional[Type[BaseModel]] = None


class AgentRegistry:
    """专业 Agent 注册表，管理所有可用的专业 Agent。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDef] = {}

    def register(self, agent: AgentDef) -> None:
        """注册一个专业 Agent。"""
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[AgentDef]:
        """根据名称获取 AgentDef，不存在则返回 None。"""
        return self._agents.get(name)

    def all_agents(self) -> list[AgentDef]:
        """返回所有已注册的专业 Agent 列表。"""
        return list(self._agents.values())

    def build_transfer_tool_schema(self) -> dict:
        """生成 transfer_to_agent 工具 schema，enum 限制为已注册 Agent 名。"""
        names = list(self._agents.keys())
        return {
            "type": "function",
            "function": {
                "name": "transfer_to_agent",
                "description": "将任务交接给专业 Agent 执行",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "enum": names,
                            "description": "专业 Agent 名称",
                        },
                        "task": {
                            "type": "string",
                            "description": "精炼的任务描述，专业 Agent 将基于此执行",
                        },
                    },
                    "required": ["agent_name", "task"],
                },
            },
        }

    def build_orchestrator_system_prompt(self) -> str:
        """构建总控 Agent 的 system prompt，列出所有专业 Agent。"""
        agent_lines = "\n".join(
            f"- {a.name}: {a.description}" for a in self._agents.values()
        )
        return (
            "你是一个总控 Agent，负责理解用户需求并协调专业 Agent 完成任务。\n"
            "你可以直接回答简单问题，或将专业任务交接给对应的 Agent。\n\n"
            f"可用的专业 Agent：\n{agent_lines}\n\n"
            "使用 transfer_to_agent 工具将任务交接给专业 Agent。"
            "交接时请提供精炼、准确的任务描述，只包含该 Agent 执行任务所需的信息。"
        )
