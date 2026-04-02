"""agent 间通信的统一结构化消息协议。

AgentMessage 强制发送方回答 WHY/WHAT/WITH/EXPECT 四个维度，
AgentResponse 统一返回格式。message_id 用于关联请求与响应。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResponseStatus(Enum):
    """agent 响应状态。"""
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_INPUT = "needs_input"


@dataclass
class AgentMessage:
    """agent 间通信的统一输入。"""
    objective: str
    task: str
    context: dict[str, Any] | str = ""
    expected_result: str | None = None
    sender: str | None = None
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class AgentResponse:
    """agent 间通信的统一输出。"""
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    status: ResponseStatus = ResponseStatus.COMPLETED
    sender: str | None = None
    message_id: str = ""

    @classmethod
    def from_graph_result(cls, result: Any) -> AgentResponse:
        """从 GraphResult 构造 AgentResponse，兼容旧 dict 格式。"""
        output = result.output
        if isinstance(output, AgentResponse):
            return output
        if isinstance(output, dict):
            return cls(
                text=output.get("text", ""),
                data=output.get("data", {}),
            )
        return cls(text=str(output))


RECEIVING_TEMPLATE = (
    "你收到了一个委托任务：\n"
    "最终目标：{objective}\n"
    "具体任务：{task}\n"
    "{context_line}"
    "{expected_result_line}"
    "\n"
    "完成后请按以下格式返回：\n"
    "第一行标注任务状态：已完成 / 信息不足 / 失败\n"
    "之后是具体结果或需要补充的信息。\n"
    "不要猜测或假设缺失的信息。"
)


def format_for_receiver(message: AgentMessage) -> str:
    """将 AgentMessage 格式化为接收方的 prompt 输入。"""
    context_line = ""
    if message.context:
        ctx = (
            message.context
            if isinstance(message.context, str)
            else json.dumps(message.context, ensure_ascii=False)
        )
        context_line = f"相关上下文：{ctx}\n"
    expected_line = (
        f"期望结果：{message.expected_result}\n"
        if message.expected_result
        else ""
    )
    return RECEIVING_TEMPLATE.format(
        objective=message.objective,
        task=message.task,
        context_line=context_line,
        expected_result_line=expected_line,
    )


def build_message_schema() -> dict:
    """生成 AgentMessage 对应的 JSON Schema，供工具 schema 复用。"""
    return {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "你的最终目标是什么（为什么需要这次协作）",
            },
            "task": {
                "type": "string",
                "description": "你需要对方具体做什么",
            },
            "context": {
                "type": "string",
                "description": "当前已知的相关信息。只填你确定知道的，不要猜测。",
            },
            "expected_result": {
                "type": "string",
                "description": "你期望对方完成后告诉你什么。如果不确定，可简要描述即可。",
            },
        },
        "required": ["objective", "task"],
    }
