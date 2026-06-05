"""@tool 装饰器 — 将函数注册为工具。"""

from __future__ import annotations

import asyncio, inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, TYPE_CHECKING, TypedDict
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from src.mgr.permission_mgr import PermissionCheckResult, PermissionContext


class ToolDict(TypedDict):
    """OpenAI function-calling 格式的工具 schema"""
    type: str
    function: Dict[str, Any]


@dataclass
class ToolPermission:
    """工具权限元数据。

    Attributes:
        readonly: 工具是否只读（不修改任何状态）。只读工具在所有模式下自动放行且始终可见。
        plan_visible: plan 模式下保持可见。非只读工具默认在 plan 模式下隐藏，
            设为 True 可让工具在 plan 模式下也保持可见（如 plan 专用文件工具）。
        specifier_arg: 用于内容级规则匹配的参数名。check() 自动提取该参数值做 fnmatch 匹配，
            同时用于构建 "always allow" session 规则。None 表示无内容级匹配。
        tips: 权限提示模板，如 "写入文件：{path}"，用于向用户展示操作详情。
        check_permissions: 工具自身安全逻辑检查函数，接收 (tool_input, ctx) 返回 PermissionCheckResult。
            仅处理工具特有的安全逻辑（如 shell 危险命令检测、file 敏感路径检查），
            不负责规则匹配——规则匹配由 check() 根据 specifier_arg 统一处理。None 表示无特殊检查。
    """
    readonly: bool = False
    plan_visible: bool = False
    specifier_arg: str | None = None
    tips: str | None = None
    check_permissions: Callable[[dict[str, Any], PermissionContext], PermissionCheckResult] | None = None


@dataclass
class ToolEntry:
    """工具的完整元数据。

    Attributes:
        name: 工具名称。
        func: 工具实现函数。
        model: Pydantic 参数模型类。
        description: 工具描述（发送给 LLM）。
        parameters_schema: OpenAI 格式的参数 schema。
        permission: 权限元数据，None 表示无特殊声明。
        raw_output: 是否跳过结果分页截断。
    """
    name: str
    func: Callable
    model: type[BaseModel]
    description: str
    parameters_schema: dict[str, Any]
    permission: ToolPermission | None = None
    raw_output: bool = False

    async def __call__(self, context: Dict[str, Any], **kwds: Any) -> Any:
        """验证参数并执行工具函数。

        Args:
            context: 包含 current_tool_call_id、deps、agent 等注入信息的上下文。
            **kwds: 工具调用参数。

        Returns:
            工具执行结果字符串。
        """
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

def format_tool_tips(tips: str | None, tool_input: dict[str, Any], fallback: str = "") -> str:
    """格式化工具提示模板，失败时返回 fallback。

    Args:
        tips: 提示模板字符串，如 "写入文件：{path}"。None 或空字符串时直接返回 fallback。
        tool_input: 工具调用参数，用于模板变量替换。
        fallback: 模板为空或格式化失败时的回退文本。

    Returns:
        格式化后的提示文本。
    """
    if not tips:
        return fallback
    try:
        return tips.format(**tool_input)
    except (AttributeError, IndexError, KeyError, ValueError):
        return tips


_registry: list[ToolEntry] = []

def tool(
    model: type[BaseModel],
    description: str,
    name: str | None = None,
    permission: ToolPermission | None = None,
    raw_output: bool = False,
) -> Callable:
    """工具注册装饰器。

    Args:
        model: Pydantic 参数模型类。
        description: 工具描述。
        name: 工具名称，默认使用函数名。
        permission: 权限元数据。
        raw_output: 是否跳过结果分页。

    Returns:
        装饰后的原函数。

    新增工具时须确认是否需要自动注入给子智能体，见 subagent_mgr._AUTO_INJECT_TOOLS。
    """
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
