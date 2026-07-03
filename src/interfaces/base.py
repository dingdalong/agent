"""UserInterface 抽象基类 — 抽象所有用户交互操作。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from src.events.types import (
    ChoiceRequested,
    CompactDelta,
    Event,
    FormQuestion,
    FormRequested,
    InputRequested,
    LLMCallCompleted,
    LLMCallStarted,
    OutputRequested,
    PermissionNotice,
    PermissionRequested,
    ResponseDelta,
    SystemStateChanged,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UserInputRequest,
)


@dataclass(frozen=True, slots=True)
class SystemState:
    """UI 可查询的系统状态。"""

    permission_mode: str = "default"


class UserInterface(ABC):
    """I/O 抽象基类，封装各 interface 共享的事件处理逻辑。"""

    def __init__(self) -> None:
        self._request_interrupt: Callable[[], None] | None = None
        self._system_state_provider: Callable[[], SystemState] | None = None
        self._active_user_request: UserInputRequest | None = None

    @contextmanager
    def watch_interrupt(self, request_interrupt: Callable[[], None]):
        """监听用户取消信号。检测到取消时调用 request_interrupt。"""
        previous = self._request_interrupt
        self._request_interrupt = request_interrupt
        try:
            yield
        finally:
            self._request_interrupt = previous

    def _request_user_interrupt(self) -> None:
        if self._request_interrupt is not None:
            self._request_interrupt()

    def cancel_active_input(self) -> bool:
        """取消当前活跃的用户输入请求。

        由应用层在中断时调用。在抽象基类中实现，所有 UI 子类共享同一取消逻辑。

        Returns:
            是否成功取消了输入请求。
        """
        if self._active_user_request is None:
            return False
        self._active_user_request.cancel()
        self._active_user_request = None
        return True

    def set_system_state_provider(self, provider: Callable[[], SystemState] | None) -> None:
        """设置 UI 查询系统状态时使用的数据源。"""

        self._system_state_provider = provider

    def get_system_state(self) -> SystemState:
        """获取当前系统状态。"""

        if self._system_state_provider is None:
            return SystemState()
        return self._system_state_provider()

    def on_system_state_changed(self) -> None:
        """系统状态变化通知。固定状态栏 UI 可在这里触发刷新。"""

        pass

    def set_permission_mode_toggle_handler(self, handler: Callable[[], None] | None) -> None:
        """设置输入期间的权限模式快捷键处理器。"""

        pass

    async def start(self) -> None:
        """启动 UI 生命周期钩子。默认实现供同步 UI 使用。"""
        pass

    async def stop(self) -> None:
        """停止 UI 生命周期钩子。默认实现供同步 UI 使用。"""
        pass

    @abstractmethod
    async def _write(self, message: str, markdown: bool = False) -> None:
        """输出文本（markdown 为真时按 Markdown 渲染）。"""
        ...

    @abstractmethod
    async def _read_input(self, prompt: str, default: str = "", markdown: bool = False) -> str:
        """读取用户输入（markdown 为真时上文提示按 Markdown 渲染）。"""
        ...

    @abstractmethod
    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
        suggested_rules: list[str] | None = None,
        mcp_server_rule: str | None = None,
    ) -> str:
        """读取权限确认结果。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            suggested_rules: 建议的 allow 规则列表，供 UI 展示。
            mcp_server_rule: MCP 工具的 server 级通配规则（mcp__<server>__*）；非空时提供"信任整个 server"选项。
        """
        ...

    @abstractmethod
    async def _read_choice(
        self, prompt: str, options: list[tuple[str, str]], default_index: int, markdown: bool = False
    ) -> str:
        """以菜单读取一次选择。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            所选项的 value；空串表示取消。
        """
        ...

    @abstractmethod
    async def _read_form(
        self, prompt: str, questions: list[FormQuestion], markdown: bool = False
    ) -> str:
        """以单屏表单读取多个问题的作答（对应 ask_user 多问题）。

        Args:
            prompt: 表单上文提示。
            questions: 问题列表，每项带可选 (value, label) 选项（无则自由文本）。
            markdown: 上文提示与问题/选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串；空串表示取消。
        """
        ...

    async def _complete_user_request(
        self,
        request: UserInputRequest | None,
        reader,
    ) -> None:
        try:
            while True:
                answer = await reader()
                normalized = answer.strip()
                if normalized:
                    if request is not None:
                        request.complete(normalized)
                    return
        except (EOFError, KeyboardInterrupt):
            self._request_user_interrupt()
        except BaseException as exc:
            if request is not None:
                request.fail(exc)

    async def _end_thinking_if_needed(self) -> None:
        """收尾思考流（若有未结束的流）。由具体 UI 覆盖实现，默认无操作。"""

    async def _end_response_if_needed(self) -> None:
        """收尾回应流（若有未结束的流）。由具体 UI 覆盖实现，默认无操作。"""

    async def _end_streams_for(self, event: Event) -> None:
        if not isinstance(event, ThinkingDelta):
            await self._end_thinking_if_needed()
        if not isinstance(event, ResponseDelta):
            await self._end_response_if_needed()

    async def on_event(self, event: Event) -> None:
        await self._end_streams_for(event)

        match event:
            case OutputRequested(content=content, markdown=markdown):
                await self._write(content, markdown=markdown)
            case InputRequested(prompt=prompt, default=default, markdown=markdown):
                self._active_user_request = event
                try:
                    next_prompt = prompt
                    next_default = default

                    async def read_input() -> str:
                        nonlocal next_prompt, next_default
                        answer = await self._read_input(next_prompt, next_default, markdown)
                        next_prompt = ""
                        next_default = ""
                        return answer if answer.strip() else ""

                    await self._complete_user_request(event, read_input)
                finally:
                    if self._active_user_request is event:
                        self._active_user_request = None
            case ChoiceRequested(prompt=prompt, options=options, default_index=default_index, markdown=markdown):
                # 选择请求允许空答案（取消），不能复用 _complete_user_request 的非空重读循环，故单次读取。
                self._active_user_request = event
                try:
                    answer = await self._read_choice(prompt, options, default_index, markdown)
                    event.complete(answer)
                except (EOFError, KeyboardInterrupt):
                    self._request_user_interrupt()
                except BaseException as exc:
                    event.fail(exc)
                finally:
                    if self._active_user_request is event:
                        self._active_user_request = None
            case FormRequested(prompt=prompt, questions=questions, markdown=markdown):
                # 表单允许空答案（Esc 取消），与 ChoiceRequested 同样单次读取、不走非空重读循环。
                self._active_user_request = event
                try:
                    answer = await self._read_form(prompt, questions, markdown)
                    event.complete(answer)
                except (EOFError, KeyboardInterrupt):
                    self._request_user_interrupt()
                except BaseException as exc:
                    event.fail(exc)
                finally:
                    if self._active_user_request is event:
                        self._active_user_request = None
            case PermissionNotice():
                await self.on_permission_notice(event)
            case PermissionRequested(tool_name=tool_name, detail=detail, suggested_rules=suggested_rules, mcp_server_rule=mcp_server_rule):
                self._active_user_request = event
                try:
                    await self._complete_user_request(
                        event,
                        lambda: self._read_permission(tool_name, detail, suggested_rules, mcp_server_rule),
                    )
                finally:
                    if self._active_user_request is event:
                        self._active_user_request = None
            case CompactDelta():
                await self.on_compact_delta(event)
            case ToolCallStarted():
                await self.on_tool_call_started(event)
            case ToolCallCompleted():
                await self.on_tool_call_completed(event)
            case LLMCallStarted():
                await self.on_llm_call_started(event)
            case LLMCallCompleted():
                await self.on_llm_call_completed(event)
            case ResponseDelta(content=content):
                await self.on_response_delta(event, content)
            case ThinkingDelta(content=content):
                await self.on_thinking_delta(event, content)
            case SystemStateChanged():
                self.on_system_state_changed()
            case _:
                await self.on_unhandled_event(event)

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        """流式回应增量。由具体 UI 覆盖实现渲染，默认无操作。"""

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        """流式思考增量。由具体 UI 覆盖实现渲染，默认无操作。"""

    async def on_unhandled_event(self, event: Event) -> None:
        pass

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        pass

    async def on_compact_delta(self, event: CompactDelta) -> None:
        pass

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        pass

    async def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        pass

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        pass

    async def on_llm_call_completed(self, event: LLMCallCompleted) -> None:
        pass
