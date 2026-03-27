"""LLM 模块类型定义。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolCallData:
    """单个工具调用数据。"""
    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    """非流式 LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None


@dataclass
class StreamChunk:
    """流式响应的单个 chunk。"""
    content: str = ""
    tool_calls_delta: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
