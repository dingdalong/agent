"""CLIInterface — 命令行交互实现。"""

import asyncio

from src.events.types import (
    Event,
    ErrorOccurred,
    InputRequested,
    LLMCallCompleted,
    LLMCallStarted,
    OutputRequested,
    PermissionNotice,
    PermissionRequested,
    ResponseDelta,
    ThinkingDelta,
)

class CLIInterface:
    """基于标准输入/输出的 CLI 交互实现。"""

    def __init__(self) -> None:
        self._in_thinking = False  # 是否正在输出思考流
        self._in_response = False  # 是否正在输出回应流
        self._ask_lock = asyncio.Lock()

    async def input(self, message: str) -> str:
        return await asyncio.to_thread(input, message)

    async def output(self, message: str) -> None:
        print(message, end="", flush=True)

    async def _read_non_empty_input(
        self,
        prompt: str,
        future: asyncio.Future[str] | None,
    ) -> None:
        try:
            next_prompt = prompt
            while True:
                answer = (await self.input(next_prompt)).strip()
                if answer:
                    if future is not None and not future.done():
                        future.set_result(answer)
                    return
                next_prompt = ""
        except Exception as exc:
            if future is not None and not future.done():
                future.set_exception(exc)

    def _end_thinking_if_needed(self) -> None:
        """如果正在输出思考流，先换行结束。"""
        if self._in_thinking:
            print(flush=True)
            self._in_thinking = False

    def _end_response_if_needed(self) -> None:
        """如果正在输出回应流，先换行结束。"""
        if self._in_response:
            print(flush=True)
            self._in_response = False

    async def on_event(self, event: Event) -> None:
        """按事件类型格式化输出到终端。"""
        # 非 ThinkingDelta 事件到来时，结束思考流
        if not isinstance(event, ThinkingDelta):
            self._end_thinking_if_needed()
        # 非 ResponseDelta 事件到来时，结束回应流
        if not isinstance(event, ResponseDelta):
            self._end_response_if_needed()

        match event:
            case ErrorOccurred(source=name, error=err):
                print(f"  ✗ {name} 错误: {err}", flush=True)
            case OutputRequested(content=c):
                await self.output(c)
            case PermissionRequested(
                tool_name=tool_name,
                detail=detail,
                future=future,
            ):
                message = (
                    f"\n⚠️  工具 '{tool_name}' 请求执行：\n"
                    f"   {detail}\n"
                    "是否允许执行？(y/n/always): "
                )
                await self._read_non_empty_input(message, future)
            case PermissionNotice(status=status, tool_name=tool_name, detail=detail):
                if status == "allow":
                    await self.output(f"[执行工具]{detail}\n")
                elif detail:
                    await self.output(f"[拒绝执行]{detail}\n")
                else:
                    await self.output(f"[拒绝执行工具]{tool_name}\n")
            case InputRequested(prompt=prompt, future=future):
                await self._read_non_empty_input(prompt, future)
            case LLMCallStarted(
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                message_count=message_count,
                tool_count=tool_count,
            ):
                print(
                    "LLM call start: "
                    f"model={model} "
                    f"estimated_input_tokens={estimated_input_tokens} "
                    f"messages={message_count} "
                    f"tools={tool_count}",
                    flush=True,
                )
            case LLMCallCompleted(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                duration_seconds=duration_seconds,
                output_tokens_per_second=output_tokens_per_second,
                total_tokens_per_second=total_tokens_per_second,
            ):
                print(
                    "LLM usage: "
                    f"model={model} "
                    f"input={self._format_optional_int(input_tokens)} "
                    f"output={self._format_optional_int(output_tokens)} "
                    f"total={self._format_optional_int(total_tokens)} "
                    f"cache_read={self._format_optional_int(cache_read_input_tokens)} "
                    f"cache_created={self._format_optional_int(cache_creation_input_tokens)} "
                    f"duration={self._format_optional_float(duration_seconds)}s "
                    f"output_tps={self._format_optional_float(output_tokens_per_second)} "
                    f"total_tps={self._format_optional_float(total_tokens_per_second)}",
                    flush=True,
                )
            case ResponseDelta(content=c):
                if not self._in_response:
                    # 回应块开头：打印前缀
                    print(f"\n{self._response_prefix(event)}", end="", flush=True)
                    self._in_response = True
                print(c, end="", flush=True)
            case ThinkingDelta(content=c):
                if not self._in_thinking:
                    # 思考块开头：打印前缀
                    print(f"\n{self._thinking_prefix(event)}", end="", flush=True)
                    self._in_thinking = True
                print(c, end="", flush=True)

    def _response_prefix(self, event: ResponseDelta) -> str:
        if event.caller_name and event.caller_uuid:
            return f"助手(name={event.caller_name}, uuid={event.caller_uuid})："
        return "助手："

    def _thinking_prefix(self, event: ThinkingDelta) -> str:
        if event.caller_name and event.caller_uuid:
            return f"💭 (name={event.caller_name}, uuid={event.caller_uuid}) "
        return "💭 "

    def _format_optional_int(self, value: int | None) -> str:
        if value is None:
            return "n/a"
        return str(value)

    def _format_optional_float(self, value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}"
