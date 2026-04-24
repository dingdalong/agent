"""LLM Provider 抽象基类与结构化输出支持。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Type
from pydantic import BaseModel, ValidationError
import json
import logging
import re
import asyncio
from src.events import EventBus
from src.tools import ToolDict

logger = logging.getLogger(__name__)

@dataclass
class LLMResponse:
    """LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    assistant_message: Optional[dict] = None

# ---- 结构化输出辅助函数 ----

def _build_output_schema(name: str, description: str, model: Type[BaseModel]) -> ToolDict:
    """从 Pydantic 模型构建 function-calling 格式的 tool schema。"""
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

def _parse_output(
    tool_calls: dict[int, dict[str, str]],
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

def _parse_json_from_text(text: str, model_cls: Type[BaseModel]) -> Optional[BaseModel]:
    """从 LLM 文本响应中提取 JSON 并解析为 Pydantic 模型。"""
    # 优先匹配 ```json ... ``` 代码块
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return model_cls(**json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
    # 尝试匹配裸 JSON 对象
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return model_cls(**json.loads(match.group()))
        except (json.JSONDecodeError, ValueError):
            pass
    return None

@dataclass
class LLMProvider(ABC):
    """所有 LLM 实现的抽象基类。"""
    api_key: str
    base_url: str
    model: str
    event_bus: EventBus
    concurrency: int = 5
    max_retries: int = 3
    timeout: float = 120.0
    supports_native_structured_output: bool = False

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def clear_reasoning_content(self, message): ...
    def estimate_tokens(self, message: list[dict]) -> int: ...
    def normalize_messages(
        self,
        message: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False
    ) -> int: ...

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        output_schema: Type[BaseModel] | None = None,
    ) -> LLMResponse: ...

    async def structured_chat(
        self,
        output_schema: Type[BaseModel],
        messages: list[dict],
        prompt: list[dict] | None = None,
        schema_name: str = "structured_output",
        schema_description: str = "结构化输出",
        temperature: float = 1.0,
    ) -> Optional[BaseModel]:
        """从对话历史中提取结构化输出。

        默认实现：Two-Pass（fake-tool + forced tool_choice）。
        支持原生结构化输出的 Provider 应覆盖此方法。
        """
        output_tool = _build_output_schema(schema_name, schema_description, output_schema)

        response = await self.chat(
            messages,
            prompt,
            tools=[output_tool],
            #tool_choice={"type": "function", "function": {"name": schema_name}},
            temperature=temperature,
        )

        result = _parse_output(response.tool_calls, schema_name, output_schema)
        if result is not None:
            return result

        return _parse_json_from_text(response.content, output_schema)
