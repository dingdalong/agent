import logging, asyncio, inspect, math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, TypedDict
from pydantic import BaseModel, ValidationError
from src.config import config

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
    raw_output: bool = False

    async def __call__(self, context: Dict[str, Any], **kwds: Any) -> Any:
        if self.sensitive:
            print(f"\n⚠️  工具 '{self.name}' 需要执行敏感操作。")
            answer = await asyncio.to_thread(input, "是否允许执行？(y/n): ")
            if not answer.strip().lower() == 'y':
                return "用户取消了操作"

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

class ToolsMgr:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._result_store: dict[str, str] = {}
        self.page_size = config["tool"]["page_size"]
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

    def get_page(self, tool_use_id: str, page: int) -> tuple[str, int, int]:
        """返回 (页面内容, 当前页码, 总页数)"""
        full = self._result_store.get(tool_use_id)
        if full is None:
            raise KeyError(f"未找到 tool_use_id={tool_use_id} 的缓存结果")
        total_pages = math.ceil(len(full) / self.page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * self.page_size
        end = start + self.page_size
        return full[start:end], page, total_pages

    def _truncate(self, result: str, tool_use_id: str) -> str:
        if len(result) <= self.page_size:
            return result
        self._result_store[tool_use_id] = result
        total_pages = math.ceil(len(result) / self.page_size)
        preview = result[:self.page_size]
        return (
            f"{preview}\n\n"
            f"...(结果已截断，共 {total_pages} 页。"
            f"使用 read_tool_result 工具传入 tool_use_id=\"{tool_use_id}\" 和 page 页码获取其余内容)"
        )

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: Dict[str, Any] | None = None) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）"""
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        result = await tool(context or {}, **arguments)
        if tool.raw_output:
            return result
        tool_use_id = (context or {}).get("tool_use_id", "")
        return self._truncate(result, tool_use_id)
