"""CLIInterface — 命令行交互实现。"""

import asyncio
import sys

from src.events.types import (
    ErrorOccurred,
    LLMCallCompleted,
    LLMCallStarted,
    PermissionNotice,
    ResponseDelta,
    ThinkingDelta,
)
from src.interfaces.base import UserInterface

class CLIInterface(UserInterface):
    """基于标准输入/输出的 CLI 交互实现。"""

    def __init__(self) -> None:
        super().__init__()
        self._ask_lock = asyncio.Lock()

    async def _read_line(self, message: str) -> str:
        print(message, end="", flush=True)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def on_stdin_ready() -> None:
            try:
                line = sys.stdin.readline()
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
                return
            if not future.done():
                future.set_result(line)

        try:
            loop.add_reader(sys.stdin.fileno(), on_stdin_ready)
        except (AttributeError, NotImplementedError, OSError):
            line = await loop.run_in_executor(None, sys.stdin.readline)
        else:
            try:
                line = await future
            finally:
                loop.remove_reader(sys.stdin.fileno())
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    async def _write(self, message: str) -> None:
        print(message, end="", flush=True)

    def _permission_prompt(self, tool_name: str, detail: str) -> str:
        return (
            f"\n⚠️  工具 '{tool_name}' 请求执行：\n"
            f"   {detail}\n"
            "是否允许执行？(y/n/always): "
        )

    async def _write_response_prefix(self, event: ResponseDelta) -> None:
        await self._write(f"\n{self._response_prefix(event)}")

    async def _write_thinking_prefix(self, event: ThinkingDelta) -> None:
        await self._write(f"\n{self._thinking_prefix(event)}")

    async def on_error(self, event: ErrorOccurred) -> None:
        print(f"  ✗ {event.source} 错误: {event.error}", flush=True)

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        if event.status == "allow":
            await self._write(f"[执行工具]{event.detail}\n")
        elif event.detail:
            await self._write(f"[拒绝执行]{event.detail}\n")
        else:
            await self._write(f"[拒绝执行工具]{event.tool_name}\n")

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        print(
            "LLM call start: "
            f"model={event.model} "
            f"estimated_input_tokens={event.estimated_input_tokens} "
            f"messages={event.message_count} "
            f"tools={event.tool_count}",
            flush=True,
        )

    async def on_llm_call_completed(self, event: LLMCallCompleted) -> None:
        print(
            "LLM usage: "
            f"model={event.model} "
            f"input={self._format_optional_int(event.input_tokens)} "
            f"output={self._format_optional_int(event.output_tokens)} "
            f"total={self._format_optional_int(event.total_tokens)} "
            f"cache_read={self._format_optional_int(event.cache_read_input_tokens)} "
            f"cache_created={self._format_optional_int(event.cache_creation_input_tokens)} "
            f"duration={self._format_optional_float(event.duration_seconds)}s "
            f"output_tps={self._format_optional_float(event.output_tokens_per_second)} "
            f"total_tps={self._format_optional_float(event.total_tokens_per_second)}",
            flush=True,
        )

    def _response_prefix(self, event: ResponseDelta) -> str:
        if event.caller_agent_type and event.caller_uuid:
            return f"助手(agent_type={event.caller_agent_type}, uuid={event.caller_uuid})："
        return "助手："

    def _thinking_prefix(self, event: ThinkingDelta) -> str:
        if event.caller_agent_type and event.caller_uuid:
            return f"💭 (agent_type={event.caller_agent_type}, uuid={event.caller_uuid}) "
        return "💭 "

    def _format_optional_int(self, value: int | None) -> str:
        if value is None:
            return "n/a"
        return str(value)

    def _format_optional_float(self, value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}"
