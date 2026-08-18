"""UserInterface 抽象基类 — 抽象所有用户交互操作。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from rich.text import Text

from src.events.types import (
    CompactDelta,
    Event,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    OutputRequested,
    PermissionNotice,
    ResponseDelta,
    PlanStateChanged,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.events.menu import (
    ChoiceInputMenu,
    ChoiceMenu,
    FormMenu,
    FormQuestion,
    InputMenu,
    ModelMenu,
    MenuRequest,
    PermissionMenu,
    TranscriptView,
    UiRequest,
    ViewRequest,
)

if TYPE_CHECKING:
    from src.mgr.session_state import SessionState


class UserInterface(ABC):
    """I/O 抽象基类，封装各 interface 共享的事件处理逻辑。"""

    def __init__(self) -> None:
        """初始化共享中断、权限模式 provider 与活跃交互状态。

        Returns:
            None.
        """
        self._request_interrupt: Callable[[], None] | None = None
        self._plan_state_provider: Callable[[], bool] | None = None
        self._model_info_provider: Callable[[], tuple[str, str] | None] | None = None
        self._active_user_request: UiRequest | None = None
        self._session_reset_in_progress = False

    @contextmanager
    def watch_interrupt(
        self,
        request_interrupt: Callable[[], None],
    ) -> Iterator[None]:
        """监听用户取消信号并在退出时恢复原处理器。

        Args:
            request_interrupt: 检测到用户取消时调用的函数。

        Returns:
            包围一次 agent 执行的上下文管理器迭代器。
        """
        previous = self._request_interrupt
        self._request_interrupt = request_interrupt
        try:
            yield
        finally:
            self._request_interrupt = previous

    def _request_user_interrupt(self) -> None:
        """调用当前已安装的用户中断处理器。

        Returns:
            None.
        """
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

    async def wait_interactions_idle(self) -> None:
        """Wait for frontend-owned asynchronous interaction cleanup.

        Returns:
            None.
        """

    @asynccontextmanager
    async def reset_session_interactions(self) -> AsyncIterator[None]:
        """Reject new UI requests while cancelling and draining old interactions.

        Yields:
            None after frontend-owned interaction cleanup has finished.
        """
        previous = self._session_reset_in_progress
        self._session_reset_in_progress = True
        try:
            self.cancel_active_input()
            await self.wait_interactions_idle()
            yield
        finally:
            self._session_reset_in_progress = previous

    async def _accept_ui_request(self, request: UiRequest) -> bool:
        """Offer a request to a frontend-specific asynchronous scheduler.

        Args:
            request: Pending UI request emitted by EventBus.

        Returns:
            True when the frontend accepted ownership and EventBus may continue.
        """
        return False

    def set_plan_state_provider(self, provider: Callable[[], bool] | None) -> None:
        """设置 UI 查询 Plan 状态时使用的数据源。

        Args:
            provider: 返回当前入口主 agent Plan 状态的函数，None 表示移除。

        Returns:
            None.
        """

        self._plan_state_provider = provider

    def get_plan_state(self) -> bool:
        """获取当前入口主 agent 的 Plan 状态。

        Returns:
            未装配 provider 时返回 False。
        """

        if self._plan_state_provider is None:
            return False
        return self._plan_state_provider()

    def on_plan_state_changed(self) -> None:
        """Plan 状态变化通知。固定状态栏 UI 可在这里触发刷新。

        Returns:
            None.
        """

        pass

    def set_plan_toggle_handler(self, handler: Callable[[], None] | None) -> None:
        """设置输入期间的 Plan 切换快捷键处理器。

        Args:
            handler: 模式轮转回调，None 表示禁用。

        Returns:
            None.
        """

        pass

    def set_model_info_provider(
        self, provider: Callable[[], tuple[str, str] | None] | None
    ) -> None:
        """设置 UI 查询当前模型与推理强度时使用的数据源。

        Args:
            provider: 返回 (模型 ID, 推理力度) 的函数，None 表示移除。

        Returns:
            None.
        """

        self._model_info_provider = provider

    def get_model_info(self) -> tuple[str, str] | None:
        """获取当前入口主 agent 的模型与推理强度。

        Returns:
            未装配 provider 时返回 None。
        """

        if self._model_info_provider is None:
            return None
        return self._model_info_provider()

    def set_input_history_provider(
        self, provider: Callable[[], list[str]] | None
    ) -> None:
        """设置输入栏历史数据源（供上键回溯）。

        Args:
            provider: 返回当前会话输入历史（时间正序）的函数，None 表示移除。

        Returns:
            None.
        """

        pass

    def refresh_input_history(self) -> None:
        """会话切换（如 /resume）后通知 UI 重新拉取输入历史。

        Returns:
            None.
        """

        pass

    async def replace_session_state(self, state: "SessionState") -> None:
        """用会话状态的可见投影替换当前聊天历史。"""

    async def start(self) -> None:
        """启动 UI 生命周期钩子。默认实现供同步 UI 使用。

        Returns:
            None.
        """
        pass

    async def stop(self) -> None:
        """停止 UI 生命周期钩子。默认实现供同步 UI 使用。

        Returns:
            None.
        """
        pass

    @abstractmethod
    async def _write(self, message: str | Text, markdown: bool = False) -> None:
        """输出文本或 Rich 文本（字符串 markdown 为真时按 Markdown 渲染）。"""
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
        reason: str = "",
    ) -> str:
        """读取权限确认结果。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            reason: 智能权限/预检拿不准的理由，弹窗前提示给用户。
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

    async def _read_model_selection(
        self,
        prompt: str,
        models: list[tuple[str, str]],
        efforts: list[str],
        default_model_index: int,
        fast_model_index: int,
        effort_index: int,
        markdown: bool = False,
    ) -> str:
        """以三次串行选择读取两个模型槽位与推理强度，返回 JSON 结果。

        非 TTY 环境没有单屏三轴交互，退化为「default 槽位模型 → fast 槽位模型 →
        推理强度」三次串行菜单，每步提示显式标注正在设置哪个槽位。

        Args:
            prompt: 菜单上文提示。
            models: 可选模型列表，每项为 (模型ID, 展示标签)。
            efforts: 可选推理强度列表（角色级单值，两槽位共用）。
            default_model_index: default 槽位的初始选中下标。
            fast_model_index: fast 槽位的初始选中下标。
            effort_index: 推理强度的初始选中下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。

        Returns:
            JSON 编码的 {"default": ..., "fast": ..., "reasoning_effort": ...} 串；
            任一步取消即整体返回空串。
        """
        head = f"{prompt}\n" if prompt else ""
        default_model = await self._read_choice(
            f"{head}设置 default 槽位模型", models, default_model_index, markdown
        )
        if not default_model:
            return ""
        fast_model = await self._read_choice(
            "设置 fast 槽位模型", models, fast_model_index, markdown
        )
        if not fast_model:
            return ""
        effort = await self._read_choice(
            "设置推理强度（角色级，两槽位共用）",
            [(value, value) for value in efforts],
            effort_index,
            False,
        )
        if not effort:
            return ""
        return json.dumps(
            {"default": default_model, "fast": fast_model, "reasoning_effort": effort},
            ensure_ascii=False,
        )

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

    @abstractmethod
    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """以「选项列表 + 一行可编辑输入」读取一次作答。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 等长对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"choice": "<value|''>", "text": "<typed|''>"} 对象串；空串表示取消。
        """
        ...

    @abstractmethod
    async def _read_transcript_view(self, uuid: str) -> str:
        """以只读分页面板查看某子 agent 的完整原始消息记录。

        Args:
            uuid: 目标子 agent 的 uuid 字符串。
        Returns:
            恒为空串（只读查看；用户 Esc 关闭）。
        """
        ...

    async def _read_nonempty_answer(self, reader: Callable[[], Awaitable[str]]) -> str:
        """Read until a non-empty answer is returned.

        Args:
            reader: Asynchronous single-attempt answer reader.

        Returns:
            Stripped non-empty answer.
        """
        while True:
            answer = await reader()
            normalized = answer.strip()
            if normalized:
                return normalized

    async def _read_menu_request(self, request: MenuRequest) -> str:
        """Read one answer-window request without settling its public future.

        Args:
            request: Pending answer request whose active frontend owns rendering.

        Returns:
            Raw normalized result expected by the EventBus caller.
        """
        match request:
            case InputMenu(prompt=prompt, default=default, markdown=markdown):
                next_prompt = prompt
                next_default = default

                async def read_input() -> str:
                    nonlocal next_prompt, next_default
                    answer = await self._read_input(next_prompt, next_default, markdown)
                    next_prompt = ""
                    next_default = ""
                    return answer

                return await self._read_nonempty_answer(read_input)
            case ChoiceMenu(prompt=prompt, options=options, default_index=default_index, markdown=markdown):
                return await self._read_choice(prompt, options, default_index, markdown)
            case ModelMenu(
                prompt=prompt,
                models=models,
                efforts=efforts,
                default_model_index=default_model_index,
                fast_model_index=fast_model_index,
                effort_index=effort_index,
                markdown=markdown,
            ):
                await self._emit_caller_banner(request.caller_agent_type, request.caller_uuid)
                return await self._read_model_selection(
                    prompt,
                    models,
                    efforts,
                    default_model_index,
                    fast_model_index,
                    effort_index,
                    markdown,
                )
            case FormMenu(prompt=prompt, questions=questions, markdown=markdown):
                await self._emit_caller_banner(request.caller_agent_type, request.caller_uuid)
                return await self._read_form(prompt, questions, markdown)
            case ChoiceInputMenu(
                prompt=prompt,
                options=options,
                descriptions=descriptions,
                input_placeholder=input_placeholder,
                default_index=default_index,
                markdown=markdown,
            ):
                await self._emit_caller_banner(request.caller_agent_type, request.caller_uuid)
                return await self._read_choice_input(
                    prompt,
                    options,
                    descriptions,
                    input_placeholder,
                    default_index,
                    markdown,
                )
            case PermissionMenu(
                tool_name=tool_name,
                detail=detail,
                reason=reason,
            ):
                await self._emit_caller_banner(request.caller_agent_type, request.caller_uuid)
                return await self._read_nonempty_answer(
                    lambda: self._read_permission(tool_name, detail, reason),
                )
            case _:
                raise TypeError(f"unsupported answer request: {type(request)!r}")

    async def _read_view_request(self, request: ViewRequest) -> str:
        """Read one view-window request without settling its public future.

        Args:
            request: Pending read-only view request.

        Returns:
            Result expected by the EventBus caller.
        """
        match request:
            case TranscriptView(uuid=uuid):
                return await self._read_transcript_view(uuid)
            case _:
                raise TypeError(f"unsupported view request: {type(request)!r}")

    async def _settle_ui_request(self, request: UiRequest) -> None:
        """Read and settle a request for serial frontends such as non-TTY input.

        Args:
            request: Pending UI request that was not accepted by an async scheduler.

        Returns:
            None.
        """
        self._active_user_request = request
        try:
            if isinstance(request, MenuRequest):
                answer = await self._read_menu_request(request)
            elif isinstance(request, ViewRequest):
                answer = await self._read_view_request(request)
            else:
                raise TypeError(f"unsupported UI request: {type(request)!r}")
            request.complete(answer)
        except (EOFError, KeyboardInterrupt):
            self._request_user_interrupt()
        except BaseException as exc:
            request.fail(exc)
        finally:
            if self._active_user_request is request:
                self._active_user_request = None

    async def _end_thinking_if_needed(self) -> None:
        """收尾思考流（若有未结束的流）。由具体 UI 覆盖实现，默认无操作。"""

    async def _end_response_if_needed(self) -> None:
        """收尾回应流（若有未结束的流）。由具体 UI 覆盖实现，默认无操作。"""

    async def _emit_caller_banner(self, caller_agent_type: str | None, caller_uuid: str | None) -> None:
        """在交互菜单弹出前标注发起该请求的 agent 身份。由具体 UI 覆盖实现，默认无操作。

        Args:
            caller_agent_type: 发起菜单的 agent 类型（主 agent 为「main」；None 表示无身份，不标注）。
            caller_uuid: 发起菜单的 agent 实例 uuid。
        """

    async def _end_streams_for(self, event: Event) -> None:
        """在事件类型切换前收尾不匹配的流式输出。

        Args:
            event: 即将处理的事件。

        Returns:
            None.
        """
        if not isinstance(event, ThinkingDelta):
            await self._end_thinking_if_needed()
        if not isinstance(event, ResponseDelta):
            await self._end_response_if_needed()

    async def on_event(self, event: Event) -> None:
        """把一个可见事件分派到共享交互流程或具体 UI hook。

        Args:
            event: OutputRouter 决定可见的事件。

        Returns:
            None.
        """
        if isinstance(event, UiRequest) and self._session_reset_in_progress:
            event.cancel()
            return
        await self._end_streams_for(event)
        if isinstance(event, UiRequest):
            if self._session_reset_in_progress:
                event.cancel()
                return
            if await self._accept_ui_request(event):
                return
            await self._settle_ui_request(event)
            return

        match event:
            case OutputRequested(content=content, markdown=markdown):
                await self._write(content, markdown=markdown)
            case PermissionNotice():
                await self.on_permission_notice(event)
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
            case LLMRetrying():
                await self.on_llm_retrying(event)
            case LLMLengthRetrying():
                await self.on_llm_length_retrying(event)
            case LLMCallFailed():
                await self.on_llm_call_failed(event)
            case ResponseDelta(content=content):
                await self.on_response_delta(event, content)
            case ThinkingDelta(content=content):
                await self.on_thinking_delta(event, content)
            case PlanStateChanged():
                self.on_plan_state_changed()
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

    async def on_llm_retrying(self, event: LLMRetrying) -> None:
        pass

    async def on_llm_length_retrying(self, event: LLMLengthRetrying) -> None:
        """处理 LLM 长度截断自动恢复事件，默认无操作。

        Args:
            event: 长度截断恢复事件。

        Returns:
            None。
        """
        pass

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        """处理 LLM 安全终态失败事件，默认无操作。

        Args:
            event: 终态失败事件。

        Returns:
            None。
        """
