import logging, asyncio, inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, TypedDict, get_type_hints
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

class ToolDict(TypedDict):
    """OpenAI function-calling 格式的工具 schema"""
    type: str
    function: Dict[str, Any]

@dataclass
class ToolEntry:
    """工具的完整元数据"""
    name: str
    func: Callable
    model: type[BaseModel]
    description: str
    parameters_schema: dict[str, Any]
    sensitive: bool = False
    confirm_template: str | None = None

    async def __call__(self, *context: Any, **kwds: Any) -> Any:
        if self.sensitive:
            """异步询问用户确认（控制台模式）"""
            print(f"\n⚠️  工具 '{self.name}' 需要执行敏感操作。")
            answer = await asyncio.to_thread(input, "是否允许执行？(y/n): ")
            if not answer.strip().lower() == 'y':
                return "用户取消了操作"

        try:
            validated_args = self.model(**kwds).model_dump()
        except ValidationError as e:
            messages = []
            for err in e.errors()[:3]:  # 最多显示前3个错误
                loc = ".".join(str(x) for x in err["loc"])
                msg = err["msg"]
                messages.append(f"{loc}: {msg}")
            result = "; ".join(messages)
            if len(e.errors()) > 3:
                result += f"... 等{len(e.errors())}个错误"
            return f"参数验证失败: {result}"

        try:
            inject = {}
            injectable = {type(obj): obj for obj in context}
            globalns = getattr(inspect.getmodule(self.func), "__dict__", {})
            localns = {cls.__name__: cls for cls in injectable}
            try:
                hints = get_type_hints(self.func, globalns=globalns, localns=localns)
            except Exception:
                hints = {}
            for param_name, param_type in hints.items():
                if param_name == "return":
                    continue
                if param_type in injectable:
                    inject[param_name] = injectable[param_type]

            if inspect.iscoroutinefunction(self.func):
                result = await self.func(**validated_args, **inject)
            else:
                result = await asyncio.to_thread(self.func, **validated_args, **inject)

            result_str = str(result)
            if len(result_str) > 500:
                result_str = result_str[:500] + "...(结果已截断)"
            return result_str
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"工具执行出错: {error_msg}"

class ToolsMgr:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        from src.tools.decorator import _registry
        for entry in _registry:
            self.register(entry)

    def register(self, tool: ToolEntry) -> None:
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已注册，跳过")
            return
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_entries(self) -> list[ToolEntry]:
        return list(self._tools.values())

    def get_schemas(self, tool_names: list[str] | None = None) -> list[ToolDict]:
        tools = self._tools.values() if tool_names is None else [
            self._tools[name] for name in tool_names if name in self._tools
        ]
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
            }
            for tool in tools
        ]

    async def execute(self, tool_name: str, arguments: Dict[str, Any], *context: Any) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）"""
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        return await tool(*context, **arguments)
