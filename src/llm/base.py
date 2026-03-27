"""LLMProvider Protocol — LLM 调用的抽象接口。"""

from typing import Protocol, runtime_checkable

from src.llm.types import LLMResponse


@runtime_checkable
class LLMProvider(Protocol):
    """所有 LLM 实现必须满足的协议。

    消费者（AgentRunner, planner, extractor, buffer）依赖此协议，
    不关心底层是 OpenAI、Claude 还是本地模型。
    """

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | None = None,
        silent: bool = False,
    ) -> LLMResponse: ...
