"""@tool 装饰器 — 将函数注册为工具。"""

from __future__ import annotations

import asyncio, inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, TypedDict
from pydantic import BaseModel, ValidationError

from src.tools.policy import BUILTIN_ORIGIN, DEFAULT_POLICY, ToolOrigin, ToolPolicy


class ToolDict(TypedDict):
    """OpenAI function-calling 格式的工具 schema"""
    type: str
    function: Dict[str, Any]


@dataclass
class ToolEntry:
    """工具的完整元数据。

    Attributes:
        name: 工具名称。
        func: 工具实现函数。
        model: Pydantic 参数模型类。
        description: 工具描述（发送给 LLM）。
        parameters_schema: OpenAI 格式的参数 schema。
        policy: 声明式授权策略；未声明时使用 REVIEW + DYNAMIC。
        origin: 工具注册来源，不参与确定性放行。
        raw_output: 是否跳过结果分页截断。
        subagent: 子 agent 可见性控制。True=自动注入（即使 agent 定义未列出）；
                  False=强制排除（即使 agent 定义为全量）；None=按 agent 的 tools 集合决定。
        feature: 所属可插拔 feature 名（如 "task"、"file"）。None 表示无归属、恒可用；
                 非 None 时，仅当该 feature 被角色启用才注入，否则从 schema 排除并在调用时拒绝。
        counts_as_work: 工具执行期间是否代表实际计算（占用本地 CPU/IO），用于状态栏耗时的人工等待暂停判定。
                        委派型（task，实际计算在子 agent）与纯人工等待型（ask_user，只等用户输入无计算）设 False，
                        其执行不计入回合活跃计算；其余工具默认 True。
    """
    name: str
    func: Callable
    model: type[BaseModel]
    description: str
    parameters_schema: dict[str, Any]
    policy: ToolPolicy = DEFAULT_POLICY
    origin: ToolOrigin = ToolOrigin("dynamic")
    raw_output: bool = False
    subagent: bool | None = None
    feature: str | None = None
    counts_as_work: bool = True

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """应用 Pydantic 默认值并返回授权和执行共用的参数。"""
        return self.model(**arguments).model_dump()

    @staticmethod
    def format_validation_error(error: ValidationError) -> str:
        messages = []
        for item in error.errors()[:3]:
            loc = ".".join(str(x) for x in item["loc"])
            messages.append(f"{loc}: {item['msg']}")
        result = "; ".join(messages)
        if len(error.errors()) > 3:
            result += f"... 等{len(error.errors())}个错误"
        return f"参数验证失败: {result}"

    async def __call__(self, context: Dict[str, Any], *, validated: bool = False, **kwds: Any) -> Any:
        """执行工具函数；调用方可传入已经验证的参数。

        Args:
            context: 包含 current_tool_call_id、deps、agent 等注入信息的上下文。
            **kwds: 工具调用参数。

        Returns:
            工具执行结果字符串。
        """
        try:
            validated_args = kwds if validated else self.validate_arguments(kwds)
        except ValidationError as error:
            return self.format_validation_error(error)

        try:
            sig = inspect.signature(self.func)
            inject = {name: context[name] for name in sig.parameters if name in context}

            if inspect.iscoroutinefunction(self.func):
                result = await self.func(**validated_args, **inject)
            else:
                result = await asyncio.to_thread(self.func, **validated_args, **inject)

            from src.tools.display import ToolResult
            if isinstance(result, ToolResult):
                return result  # 保留 ToolResult，ToolsMgr 提取 .text
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
    policy: ToolPolicy | None = None,
    raw_output: bool = False,
    subagent: bool | None = None,
    feature: str | None = None,
    counts_as_work: bool = True,
) -> Callable:
    """工具注册装饰器。

    Args:
        model: Pydantic 参数模型类。
        description: 工具描述。
        name: 工具名称，默认使用函数名。
        policy: 声明式授权策略，未声明时保守使用 REVIEW + DYNAMIC。
        raw_output: 是否跳过结果分页。
        subagent: 子 agent 可见性。True=自动注入；False=强制排除；None=按 agent 定义决定。
        feature: 所属可插拔 feature 名。None 表示无归属、恒可用；非 None 时随该 feature 的启用与否注入或排除。
        counts_as_work: 工具执行期间是否代表实际计算。委派型与纯人工等待型设 False，不计入回合活跃计算；默认 True。

    Returns:
        装饰后的原函数。
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
            policy=policy or DEFAULT_POLICY,
            origin=BUILTIN_ORIGIN,
            raw_output=raw_output,
            subagent=subagent,
            feature=feature,
            counts_as_work=counts_as_work,
        )
        _registry.append(entry)
        return func

    return decorator
