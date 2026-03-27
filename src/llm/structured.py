"""structured_output — 结构化输出。

利用 function calling 机制约束 LLM 按 Pydantic 模型输出结构化 JSON。
"""

import json
import logging
from typing import Dict, Optional, Type

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


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
