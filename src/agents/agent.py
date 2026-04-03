"""Agent 数据模型 — 声明式定义一个 agent 是什么、能做什么。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Type

from pydantic import BaseModel

from src.graph.messages import AgentMessage, AgentResponse
from src.guardrails.base import Guardrail


@dataclass
class HandoffRequest:
    """Agent 请求将任务永久交接到另一个 agent。"""

    target: str
    message: AgentMessage


@dataclass
class AgentResult:
    """单个 agent 的执行结果。"""

    response: AgentResponse
    handoff: Optional[HandoffRequest] = None

    @property
    def text(self) -> str:
        return self.response.text

    @property
    def data(self) -> dict:
        return self.response.data


@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        name: 唯一标识。
        description: 一句话描述，用于 handoff 工具生成。
        instructions: 系统提示，支持字符串或 Callable[[RunContext], str] 动态生成。
        tools: 允许使用的工具名列表。
        handoffs: 可 handoff 到的 agent 名列表。
        output_model: 结构化输出的 Pydantic 模型。
        input_guardrails: 输入护栏列表。
        output_guardrails: 输出护栏列表。
    """

    name: str
    description: str
    instructions: str | Callable[..., str]
    task: str | None = None  # 工作流步骤专用：覆盖 context.input 作为 user message
    tools: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    output_model: Optional[Type[BaseModel]] = None
    input_guardrails: list[Guardrail] = field(default_factory=list)
    output_guardrails: list[Guardrail] = field(default_factory=list)
