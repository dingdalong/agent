import logging, math
from typing import Any, Dict
from src.config import config
from src.tools import ToolDict, ToolEntry

logger = logging.getLogger(__name__)

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

    def get_page(self, tool_call_id: str, page: int) -> tuple[str, int, int]:
        """返回 (页面内容, 当前页码, 总页数)"""
        full = self._result_store.get(tool_call_id)
        if full is None:
            raise KeyError(f"未找到 tool_call_id={tool_call_id} 的缓存结果")
        total_pages = math.ceil(len(full) / self.page_size)
        if page < 1 or page > total_pages:
            raise ValueError(f"页码超出范围：page={page}，总页数为 {total_pages}")
        start = (page - 1) * self.page_size
        end = start + self.page_size
        return full[start:end], page, total_pages

    def _truncate(self, result: str, tool_call_id: str) -> str:
        if len(result) <= self.page_size:
            return result
        self._result_store[tool_call_id] = result
        total_pages = math.ceil(len(result) / self.page_size)
        preview = result[:self.page_size]
        return (
            f"{preview}\n\n"
            f"[TOOL_RESULT_TRUNCATED]\n"
            f"[tool_result 分页提示]\n"
            f"工具调用 id: \"{tool_call_id}\"\n"
            f"结果已截断为分页返回。\n"
            f"这是该工具结果的第 1/{total_pages} 页，当前只读取了第 1 页。\n"
            f"第 1 页已包含在本次返回中，继续读取时从 page=2 开始。\n"
            f"理解和分析时应将各页视为同一个连续的工具结果，无需输出合并后的完整文本。\n"
            f"继续调用 read_tool_result(tool_call_id=\"{tool_call_id}\", page=2) 读取下一页。"
        )

    async def execute(self, tool_name: str, arguments: Dict[str, Any], context: Dict[str, Any] | None = None) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）"""
        if tool_name not in self._tools:
            return f"错误：未知工具 '{tool_name}'"

        tool = self._tools[tool_name]
        result = await tool(context or {}, **arguments)
        if tool.raw_output:
            return result

        tool_call_id = (context or {}).get("current_tool_call_id")
        if not tool_call_id:
            return result

        return self._truncate(result, tool_call_id)
