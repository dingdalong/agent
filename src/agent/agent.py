from dataclasses import dataclass, field
from typing import Callable, Optional, Type, Any
from pydantic import BaseModel
from src.singleton import ui, event_bus, llm
from src.config import config
from src.tools import tools_mgr
import json

max_tool_rounds = config["llm"]["max_tool_rounds"]

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

    async def run(self, input: str) -> str:
        if callable(self.prompt):
            prompt = self.prompt(input)
        else:
            prompt = self.prompt

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": input},
        ]

        tool_list = tools_mgr.get_schemas()
        final_text = ""
        for round_idx in range(max_tool_rounds):
            response = await llm.chat(messages, tool_list)
            if response is not None:
                content, tool_calls, = response.content, response.tool_calls

            if not tool_calls:
                final_text = content
                break

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls.values()
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls.values():
                tool_name = tc["name"]
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}

                result_text = await tools_mgr.execute(tool_name, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result_text),
                })
        else:
            # 超过 max_tool_rounds
            response = await llm.chat(messages)
            if response is not None:
                final_text = response.content

        return final_text
