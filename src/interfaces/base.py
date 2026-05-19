"""UserInterface 抽象基类 — 抽象所有用户交互操作。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.events.types import (
    Event,
    ErrorOccurred,
    InputInterrupted,
    InputRequested,
    LLMCallCompleted,
    LLMCallStarted,
    OutputRequested,
    PermissionNotice,
    PermissionRequested,
    ResponseDelta,
    ThinkingDelta,
    UserInputRequest,
)


class UserInterface(ABC):
    """I/O 抽象基类，封装各 interface 共享的事件处理逻辑。"""

    def __init__(self) -> None:
        self._in_thinking = False
        self._in_response = False

    @abstractmethod
    async def _read_line(self, prompt: str) -> str:
        """读取一行用户输入。"""
        ...

    @abstractmethod
    async def _write(self, message: str) -> None:
        """输出文本。"""
        ...

    @abstractmethod
    def _permission_prompt(self, tool_name: str, detail: str) -> str:
        """格式化权限请求提示。"""
        ...

    @abstractmethod
    async def _write_response_prefix(self, event: ResponseDelta) -> None:
        """输出回应流前缀。"""
        ...

    @abstractmethod
    async def _write_thinking_prefix(self, event: ThinkingDelta) -> None:
        """输出思考流前缀。"""
        ...

    async def _read_non_empty_input(
        self,
        prompt: str,
        request: UserInputRequest | None,
    ) -> None:
        try:
            next_prompt = prompt
            while True:
                answer = (await self._read_line(next_prompt)).strip()
                if answer:
                    if request is not None:
                        request.complete(answer)
                    return
                next_prompt = ""
        except EOFError:
            if request is not None:
                request.interrupt()
        except Exception as exc:
            if request is not None:
                request.fail(exc)
        except BaseException as exc:
            if request is not None:
                if isinstance(exc, KeyboardInterrupt):
                    request.interrupt()
                else:
                    request.fail(exc)

    async def _end_thinking_if_needed(self) -> None:
        if self._in_thinking:
            await self._write("\n")
            self._in_thinking = False

    async def _end_response_if_needed(self) -> None:
        if self._in_response:
            await self._write("\n")
            self._in_response = False

    async def _end_streams_for(self, event: Event) -> None:
        if not isinstance(event, ThinkingDelta):
            await self._end_thinking_if_needed()
        if not isinstance(event, ResponseDelta):
            await self._end_response_if_needed()

    async def on_event(self, event: Event) -> None:
        await self._end_streams_for(event)

        match event:
            case OutputRequested(content=content):
                await self._write(content)
            case ErrorOccurred():
                await self.on_error(event)
            case InputRequested(prompt=prompt):
                await self._read_non_empty_input(prompt, event)
            case PermissionNotice():
                await self.on_permission_notice(event)
            case PermissionRequested(tool_name=tool_name, detail=detail):
                await self._read_non_empty_input(
                    self._permission_prompt(tool_name, detail),
                    event,
                )
            case LLMCallStarted():
                await self.on_llm_call_started(event)
            case LLMCallCompleted():
                await self.on_llm_call_completed(event)
            case ResponseDelta(content=content):
                await self.on_response_delta(event, content)
            case ThinkingDelta(content=content):
                await self.on_thinking_delta(event, content)
            case _:
                await self.on_unhandled_event(event)

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        if not self._in_response:
            await self._write_response_prefix(event)
            self._in_response = True
        await self._write(content)

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        if not self._in_thinking:
            await self._write_thinking_prefix(event)
            self._in_thinking = True
        await self._write(content)

    async def on_unhandled_event(self, event: Event) -> None:
        pass

    async def on_error(self, event: ErrorOccurred) -> None:
        pass

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        pass

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        pass

    async def on_llm_call_completed(self, event: LLMCallCompleted) -> None:
        pass
