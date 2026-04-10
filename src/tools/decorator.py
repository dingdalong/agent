"""@tool 装饰器 — 将函数注册为工具。"""

from typing import Callable
from pydantic import BaseModel
from src.tools.tools_mgr import tools_mgr, ToolEntry

def tool(
    model: type[BaseModel],
    description: str,
    name: str | None = None,
    sensitive: bool = False,
    confirm_template: str | None = None,
) -> Callable:
    """工具注册装饰器"""
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__

        # 生成参数 schema
        model_schema = model.model_json_schema()
        if model_schema.get("type") == "object":
            parameters_schema = model_schema
        else:
            parameters_schema = {
                "type": "object",
                "properties": {"input": model_schema},
                "required": ["input"],
            }
        parameters_schema.pop("description", None)

        entry = ToolEntry(
            name=tool_name,
            func=func,
            model=model,
            description=description,
            parameters_schema=parameters_schema,
            sensitive=sensitive,
            confirm_template=confirm_template,
        )
        tools_mgr.register(entry)
        return func

    return decorator
