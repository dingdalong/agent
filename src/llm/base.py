from typing import Protocol, runtime_checkable
from typing import Dict, Optional, Type
from dataclasses import dataclass, field
from typing import Optional
from pydantic import BaseModel, ValidationError
import json, logging

logger = logging.getLogger(__name__)

@dataclass
class LLMResponse:
    """非流式 LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None

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
        tool_choice: str | dict | None = None,
    ) -> LLMResponse | None: ...

def build_output_schema(name: str, description: str, model: Type[BaseModel]) -> dict:
    """从 Pydantic 模型构建结构化输出的 tool schema。"""
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }

def parse_output(
    tool_calls: Dict[int, Dict[str, str]],
    name: str,
    model: Type[BaseModel],
) -> Optional[BaseModel]:
    """从 LLM 的 tool_calls 中解析结构化输出。"""
    for tc in tool_calls.values():
        if tc.get("name") == name:
            try:
                data = json.loads(tc["arguments"])
                return model(**data)
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"结构化输出 '{name}' 解析失败: {e}")
                return None
    return None
