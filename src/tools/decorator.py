"""@tool 装饰器 — 将函数注册为工具。"""

import asyncio, inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, TypedDict
from pydantic import BaseModel, ValidationError

class ToolDict(TypedDict):
    """OpenAI function-calling 格式的工具 schema"""
    type: str
    function: Dict[str, Any]

@dataclass
class PermissionRule:
    """单条工具权限规则。"""
    permission: str
    args: dict[str, str | list[str]] | None = None


@dataclass
class ToolPermission:
    """工具权限元数据。"""
    tips: str | None = None
    args: list[str] | None = None
    rules: list[PermissionRule] | None = None


@dataclass
class ToolEntry:
    """工具的完整元数据"""
    name: str
    func: Callable
    model: type[BaseModel]
    description: str
    parameters_schema: dict[str, Any]
    # 工具权限元数据。None 表示无内置权限规则，未匹配时询问。
    permission: ToolPermission | None = None
    raw_output: bool = False

    async def __call__(self, context: Dict[str, Any], **kwds: Any) -> Any:
        try:
            validated_args = self.model(**kwds).model_dump()
        except ValidationError as e:
            messages = []
            for err in e.errors()[:3]:
                loc = ".".join(str(x) for x in err["loc"])
                msg = err["msg"]
                messages.append(f"{loc}: {msg}")
            result = "; ".join(messages)
            if len(e.errors()) > 3:
                result += f"... 等{len(e.errors())}个错误"
            return f"参数验证失败: {result}"

        try:
            sig = inspect.signature(self.func)
            inject = {name: context[name] for name in sig.parameters if name in context}

            if inspect.iscoroutinefunction(self.func):
                result = await self.func(**validated_args, **inject)
            else:
                result = await asyncio.to_thread(self.func, **validated_args, **inject)

            return str(result)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"工具执行出错: {error_msg}"

_registry: list[ToolEntry] = []

def tool(
    model: type[BaseModel],
    description: str,
    name: str | None = None,
    permission: ToolPermission | None = None,
    raw_output: bool = False,
) -> Callable:
    """工具注册装饰器"""
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__

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
            permission=permission,
            raw_output=raw_output,
        )
        _registry.append(entry)
        return func

    return decorator
