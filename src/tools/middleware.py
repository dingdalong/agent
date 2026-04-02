"""中间件链 — 工具执行的横切关注点。"""

from typing import Awaitable, Callable

from .registry import ToolRegistry

# 类型定义
NextFn = Callable[[str, dict], Awaitable[str]]
Middleware = Callable[[str, dict, NextFn], Awaitable[str]]


def build_pipeline(execute_fn: NextFn, middlewares: list[Middleware]) -> NextFn:
    """构建中间件链。

    middlewares 列表中第一个中间件最先执行（最外层）。
    """
    pipeline = execute_fn
    for mw in reversed(middlewares):
        prev = pipeline

        async def wrapped(name, args, _prev=prev, _mw=mw):
            return await _mw(name, args, _prev)

        pipeline = wrapped
    return pipeline


def sensitive_confirm_middleware(registry: ToolRegistry, interaction) -> Middleware:
    """敏感工具执行前需要用户确认。interaction 为 UserInteractionService 实例。"""

    async def middleware(name: str, args: dict, next_fn: NextFn) -> str:
        entry = registry.get(name)
        if entry and entry.sensitive:
            if entry.confirm_template:
                try:
                    msg = entry.confirm_template.format(**args)
                except KeyError:
                    msg = f"执行敏感操作: {name}"
            else:
                msg = f"执行敏感操作: {name}"

            confirmed = await interaction.confirm(msg)
            if not confirmed:
                return "用户取消了操作"

        return await next_fn(name, args)

    return middleware


def truncate_middleware(max_length: int = 2000) -> Middleware:
    """截断过长的结果"""

    async def middleware(name: str, args: dict, next_fn: NextFn) -> str:
        result = await next_fn(name, args)
        if len(result) > max_length:
            return result[:max_length] + f"\n...(结果已截断，共 {len(result)} 字符)"
        return result

    return middleware


def error_handler_middleware() -> Middleware:
    """捕获异常，返回错误字符串"""

    async def middleware(name: str, args: dict, next_fn: NextFn) -> str:
        try:
            return await next_fn(name, args)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"工具 '{name}' 执行出错: {error_msg}"

    return middleware
