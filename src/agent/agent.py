from dataclasses import dataclass
from typing import Callable, Any, Type
from pydantic import BaseModel, ValidationError
from src.singleton import ui, event_bus, llm
from src.config import config
from src.tools import tools_mgr as _global_tools_mgr, ToolDict
from src.tools.tools_mgr import ToolsMgr
import json
import logging

logger = logging.getLogger(__name__)

max_tool_rounds = config["llm"]["max_tool_rounds"]

@dataclass
class StructOutputConfig:
    """Agent.run 的结构化输出配置。"""
    model_cls: Type[BaseModel]
    schema_name: str = "structured_output"
    schema_desc: str = "结构化输出"


@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        name: 唯一标识。
        description: 一句话描述
        prompt: 系统提示
    """

    name: str
    description: str
    prompt: str | Callable[..., str]
    tools_mgr: ToolsMgr | None = field(default=None, repr=False)
    _tool_list: list[ToolDict] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        if self.tools_mgr is None : self.tools_mgr = _global_tools_mgr
        self._tool_list = self.tools_mgr.get_schemas()

    async def clear_reasoning_content(self, messages):
        for message in messages:
            # 处理对象（有 reasoning_content 属性）
            if hasattr(message, 'reasoning_content'):
                message.reasoning_content = None
            # 处理字典（有 'reasoning_content' 键）
            elif isinstance(message, dict) and 'reasoning_content' in message:
                message['reasoning_content'] = None

    async def run(
        self,
        input: str,
        struct_output: StructOutputConfig | None = None,
    ) -> str | BaseModel:
        if callable(self.prompt):
            prompt = self.prompt(input)
        else:
            prompt = self.prompt

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": input},
        ]

        schema_cls = struct_output.model_cls if struct_output else None
        final_text = ""
        for round_idx in range(max_tool_rounds):
            response = await llm.chat(messages, self._tool_list, output_schema=schema_cls)
            content, tool_calls = response.content, response.tool_calls

            if not tool_calls:
                final_text = content
                break

            messages.append(response.assistant_message)

            for tc in tool_calls.values():
                tool_name = tc["name"]
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}

                result_text = await self.tools_mgr.execute(tool_name, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result_text),
                })
        else:
            # 超过 max_tool_rounds
            response = await llm.chat(messages)
            final_text = response.content

        #移除思考内容
        await self.clear_reasoning_content(messages)

        # 结构化输出解析
        if struct_output is not None:
            # 原生 Provider：final_text 已是受约束的 JSON，直接解析
            if llm.supports_native_structured_output and final_text:
                try:
                    return struct_output.model_cls.model_validate_json(final_text)
                except (ValidationError, Exception) as e:
                    logger.debug(f"原生结构化输出解析失败，回退到 structured_chat: {e}")
            # 非原生 或 解析失败：Two-Pass 兜底
            result = await llm.structured_chat(
                messages=messages,
                output_schema=struct_output.model_cls,
                schema_name=struct_output.schema_name,
                schema_description=struct_output.schema_desc,
            )
            if result is not None:
                return result

        return final_text
