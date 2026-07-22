"""Agent 状态机统一收口 LLM 终态失败的回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.states import AgentState, RunContext, RunResult
from src.llm.base import LLMResponse
from src.llm.errors import LLMCallError, LLMErrorInfo, LLMErrorKind
from src.mgr.compact_mgr import CompactMgr
from src.mgr.role_mgr import AgentManifest
from src.mgr.subagent_mgr import SubAgentMgr


class RecordingEventBus:
    """记录状态事件并按顺序提供交互输入。"""

    def __init__(self, inputs: list[str] | None = None) -> None:
        """初始化事件总线。

        Args:
            inputs: request_input 每次返回的输入序列。

        Returns:
            None。
        """
        self.inputs = iter(inputs or [])
        self.events: list[object] = []
        self.outputs: list[str] = []

    async def emit(self, event: object) -> None:
        """记录一个事件。

        Args:
            event: 待记录事件。

        Returns:
            None。
        """
        self.events.append(event)

    async def request_input(self, prompt: str, default: str = "") -> str:
        """返回下一条预设输入。

        Args:
            prompt: 输入提示文本。
            default: 输入默认值。

        Returns:
            下一条预设输入。
        """
        del prompt, default
        return next(self.inputs)

    async def request_output(self, content: str, **kwargs: object) -> None:
        """记录请求输出文本。

        Args:
            content: 输出文本。
            **kwargs: 输出选项。

        Returns:
            None。
        """
        del kwargs
        self.outputs.append(content)


class NoopReminder:
    """不生成任何提示词的测试 reminder。"""

    def build_turn_start_instructions(self, mode: object) -> str:
        """返回空轮次提示词。

        Args:
            mode: 当前权限模式。

        Returns:
            空字符串。
        """
        del mode
        return ""

    def collect_post_round_messages(self, mode: object) -> list[dict]:
        """返回空的工具轮后消息。

        Args:
            mode: 当前权限模式。

        Returns:
            空消息列表。
        """
        del mode
        return []


class StaticPromptMgr:
    """返回固定系统提示词的测试 prompt manager。"""

    def build(self) -> list[dict]:
        """构建固定系统提示词。

        Returns:
            固定系统提示词。
        """
        return [{"role": "system", "content": "test"}]


class SequenceLLM:
    """按顺序抛出异常或返回响应的测试 LLM。"""

    context_limit = 1000

    def __init__(self, outcomes: list[LLMCallError | LLMResponse]) -> None:
        """初始化调用结果序列。

        Args:
            outcomes: 每次 chat 使用的异常或响应。

        Returns:
            None。
        """
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> LLMResponse:
        """抛出或返回下一项结果。

        Args:
            **kwargs: Agent 传入的调用参数。

        Returns:
            下一项成功响应。

        Raises:
            LLMCallError: 下一项为终态错误时抛出。
        """
        recorded = dict(kwargs)
        messages = recorded.get("messages")
        if isinstance(messages, list):
            recorded["messages"] = [dict(message) for message in messages]
        self.requests.append(recorded)
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, LLMCallError):
            raise outcome
        return outcome

    def normalize_messages(self, messages: list[dict]) -> list[dict]:
        """复制消息列表。

        Args:
            messages: 待复制消息。

        Returns:
            新的消息列表。
        """
        return list(messages)

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[dict] | None = None,
    ) -> int:
        """返回固定 token 估算。

        Args:
            messages: 会话消息。
            prompt: 系统提示词。
            tools: 工具 schema。

        Returns:
            固定值 1。
        """
        del messages, prompt, tools
        return 1

    def clear_reasoning_content(self, messages: list[dict]) -> None:
        """保持历史消息不变。

        Args:
            messages: 本轮消息。

        Returns:
            None。
        """
        del messages


def _terminal_error(
    kind: LLMErrorKind,
    message: str,
    *,
    retryable: bool = False,
    attempts: int = 1,
) -> LLMCallError:
    """构造已安全化的 LLM 终态错误。

    Args:
        kind: 稳定错误分类。
        message: 安全错误摘要。
        retryable: 错误本身是否可重试。
        attempts: 已执行尝试次数。

    Returns:
        LLMCallError 实例。
    """
    return LLMCallError(
        info=LLMErrorInfo(
            kind=kind,
            message=message,
            retryable=retryable,
            original_exception_type="UnsafeProviderError",
        ),
        attempts=attempts,
        diagnostic_id="llm_internal_only",
    )


def _success_response(text: str = "成功") -> LLMResponse:
    """构造普通文本成功响应。

    Args:
        text: 响应正文。

    Returns:
        完整 LLM 响应。
    """
    return LLMResponse(
        content=text,
        finish_reason="stop",
        assistant_message={"role": "assistant", "content": text},
    )


def _runtime_agent(
    llm: SequenceLLM,
    *,
    event_bus: RecordingEventBus | None = None,
) -> Agent:
    """构造覆盖对话状态机所需字段的最小 Agent。

    Args:
        llm: 测试 LLM。
        event_bus: 可选事件总线。

    Returns:
        未运行 __post_init__ 的最小 Agent。
    """
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.agent_type = "main"
    agent.description = ""
    agent.history = []
    agent.llm = llm
    agent._tools_schemas = []
    agent.enable_thinking = True
    agent.permission_mode = None
    agent.is_subagent = False
    agent._pending_input = ""
    agent._prompt_mgr = StaticPromptMgr()
    agent._compact_mgr = SimpleNamespace(auto_compact_size=0)
    agent._reminder_mgr = NoopReminder()
    agent.deps = SimpleNamespace(
        event_bus=event_bus or RecordingEventBus(),
        hooks_mgr=None,
        session_mgr=None,
    )
    agent._handlers = {
        AgentState.REQUEST_INPUT: agent._on_request_input,
        AgentState.CHECK_COMPACT: agent._on_check_compact,
        AgentState.LLM_CALL: agent._on_llm_call,
        AgentState.PROCESS_RESPONSE: agent._on_process_response,
        AgentState.LENGTH_RETRY: agent._on_length_retry,
        AgentState.CHECK_STOP: agent._on_check_stop,
        AgentState.SUMMARIZE_EXIT: agent._on_summarize_exit,
        AgentState.CONTEXT_OVERFLOW: agent._on_context_overflow,
        AgentState.LLM_FAILURE: agent._on_llm_failure,
    }
    return agent


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (_terminal_error(LLMErrorKind.AUTHENTICATION, "认证配置无效"), "authentication"),
        (_terminal_error(LLMErrorKind.SERVICE, "服务暂时不可用", retryable=True, attempts=3), "service"),
    ],
)
def test_terminal_chat_failure_returns_safe_result_and_keeps_only_user_message(
    error: LLMCallError,
    expected_kind: str,
) -> None:
    """永久失败和重试耗尽都返回安全结果且不写入伪 assistant。"""
    error.__cause__ = RuntimeError("Bearer sk-secret request_body={'api_key':'secret'}")
    agent = _runtime_agent(SequenceLLM([error]))

    result = asyncio.run(agent.run("原始问题"))

    assert result.llm_error is error.info
    assert expected_kind in result.final_text
    assert error.info.message in result.final_text
    assert "请" in result.final_text
    assert "sk-secret" not in result.final_text
    assert "request_body" not in result.final_text
    assert "diagnostic" not in result.final_text
    assert agent.history == [{"role": "user", "content": "原始问题"}]


def test_context_limit_uses_dedicated_state_without_fake_assistant() -> None:
    """上下文限制错误进入专用状态并保留本轮用户消息。"""
    error = _terminal_error(LLMErrorKind.CONTEXT_LIMIT, "输入超出模型上下文窗口")
    agent = _runtime_agent(SequenceLLM([error]))

    result = asyncio.run(agent.run("很长的问题"))

    assert result.llm_error is error.info
    assert "上下文" in result.final_text
    assert agent.history == [{"role": "user", "content": "很长的问题"}]
    transitions = [
        (event.from_state, event.to_state)
        for event in agent.deps.event_bus.events
        if hasattr(event, "from_state")
    ]
    assert ("llm_call", "context_overflow") in transitions


def test_length_continuation_failure_rolls_back_recovery_artifacts() -> None:
    """长度续写调用终态失败时只保留原始 user，残片仅存在于转录。

    Returns:
        None。
    """
    error = _terminal_error(LLMErrorKind.AUTHENTICATION, "续写调用认证失败")
    llm = SequenceLLM([
        LLMResponse(
            content="已显示但尚未完成的正文",
            finish_reason="length",
            assistant_message={"role": "assistant", "content": "已显示但尚未完成的正文"},
        ),
        error,
    ])
    agent = _runtime_agent(llm)

    result = asyncio.run(agent.run("生成长篇报告"))

    assert result.llm_error is error.info
    assert llm.calls == 2
    assert llm.requests[1]["messages"][-2:] == [
        {"role": "assistant", "content": "已显示但尚未完成的正文"},
        {
            "role": "user",
            "content": "输出达到长度上限。请从中断处直接继续，不要回顾、不要重复，必要时可以从半句话接续。",
        },
    ]
    assert agent.history == [{"role": "user", "content": "生成长篇报告"}]


@pytest.mark.parametrize("state_name", ["COMPACT", "POST_ROUND"])
def test_auto_and_manual_compact_failures_are_caught_at_turn_boundary(
    state_name: str,
) -> None:
    """自动和手动 compact 的 LLM 失败均由单轮状态机统一收口。"""
    error = _terminal_error(LLMErrorKind.SERVICE, f"{state_name} 摘要失败")

    class FailingCompact:
        """始终抛出终态错误的 compact manager。"""

        auto_compact_size = 1

        async def compact_history(
            self,
            messages: list[dict],
            focus: str | None = None,
        ) -> object:
            """抛出预设终态错误。

            Args:
                messages: 待压缩消息。
                focus: 可选压缩重点。

            Returns:
                不返回。

            Raises:
                LLMCallError: 固定终态错误。
            """
            del messages, focus
            raise error

    agent = _runtime_agent(SequenceLLM([]))
    agent._compact_mgr = FailingCompact()
    if state_name == "COMPACT":
        start_state = AgentState.COMPACT
        handler = agent._on_compact
        ctx = RunContext(messages=[{"role": "user", "content": "自动压缩"}])
    else:
        start_state = AgentState.POST_ROUND
        handler = agent._on_post_round
        ctx = RunContext(
            messages=[{"role": "user", "content": "手动压缩"}],
            manual_compact=True,
            compact_focus="重点",
        )
    agent._handlers[start_state] = handler

    result = asyncio.run(agent._run_single_turn(ctx, start_state))

    assert result.llm_error is error.info
    assert error.info.message in result.final_text
    assert all(message.get("role") != "assistant" for message in ctx.messages)


def test_paginated_compact_summary_failure_reaches_turn_boundary(tmp_path: Path) -> None:
    """CompactMgr 分页摘要内的终态错误穿透到 Agent 单轮边界。"""
    error = _terminal_error(LLMErrorKind.RATE_LIMIT, "分页摘要重试耗尽", retryable=True, attempts=3)

    class PagedSummaryLLM:
        """强制大消息分页并在第一页摘要时失败的 LLM。"""

        context_limit = 5000

        def __init__(self) -> None:
            self.split_calls = 0
            self.chat_calls = 0

        def estimate_tokens(
            self,
            messages: list[dict],
            prompt: list[dict] | None = None,
            tools: list[dict] | None = None,
        ) -> int:
            """以字符数近似 token 数量。

            Args:
                messages: 待估算消息。
                prompt: 可选系统提示词。
                tools: 可选工具 schema。

            Returns:
                所有消息正文的字符总数。
            """
            del prompt, tools
            return sum(len(str(message.get("content", ""))) for message in messages)

        def split_page(self, text: str) -> list[str]:
            """把序列化原子块无损拆成固定小页。

            Args:
                text: 完整序列化原子块。

            Returns:
                可重新拼接为原文的分页。
            """
            self.split_calls += 1
            return [text[index:index + 2500] for index in range(0, len(text), 2500)]

        async def chat(self, **kwargs: object) -> LLMResponse:
            """记录摘要调用并抛出终态错误。

            Args:
                **kwargs: 摘要调用参数。

            Returns:
                不返回。

            Raises:
                LLMCallError: 固定终态错误。
            """
            del kwargs
            self.chat_calls += 1
            raise error

    paged_llm = PagedSummaryLLM()
    compact_mgr = CompactMgr(llm=paged_llm, workdir=tmp_path)
    agent = _runtime_agent(SequenceLLM([]))

    async def summarize_handler(ctx: RunContext) -> AgentState:
        """调用实际 CompactMgr 的分页摘要路径。

        Args:
            ctx: 当前运行上下文。

        Returns:
            摘要成功时结束单轮。
        """
        await compact_mgr.summarize_history(
            messages_to_summarize=[{"role": "assistant", "content": "x" * 10000}],
        )
        return AgentState.DONE

    agent._handlers[AgentState.COMPACT] = summarize_handler
    result = asyncio.run(agent._run_single_turn(RunContext(messages=[]), AgentState.COMPACT))

    assert paged_llm.split_calls == 1
    assert paged_llm.chat_calls == 1
    assert result.llm_error is error.info


def test_exit_summary_failure_is_caught_without_fake_assistant() -> None:
    """退出总结失败保持原历史，后续重试不携带内部总结指令。"""
    error = _terminal_error(LLMErrorKind.SERVICE, "退出总结服务不可用")
    llm = SequenceLLM([error, _success_response("继续成功")])
    agent = _runtime_agent(llm)
    original_messages = [{"role": "user", "content": "原始任务"}]
    agent.history = original_messages
    ctx = RunContext(
        messages=agent.history,
        prompt=[{"role": "system", "content": "test"}],
    )

    result = asyncio.run(agent._run_single_turn(ctx, AgentState.SUMMARIZE_EXIT))

    assert result.llm_error is error.info
    assert error.info.message in result.final_text
    assert agent.history is original_messages
    assert agent.history == [{"role": "user", "content": "原始任务"}]

    retry_result = asyncio.run(agent.run("重试"))

    assert retry_result.final_text == "继续成功"
    retry_messages = llm.requests[-1]["messages"]
    assert isinstance(retry_messages, list)
    assert all("由于对话上下文过长" not in str(message.get("content", "")) for message in retry_messages)


def test_main_agent_accepts_retry_input_after_failed_turn() -> None:
    """主 agent 一轮失败后自然读取下一轮输入并可成功继续。"""
    error = _terminal_error(LLMErrorKind.SERVICE, "临时服务故障")
    event_bus = RecordingEventBus(["第一次", "重试", "exit"])
    agent = _runtime_agent(
        SequenceLLM([error, _success_response("重试成功")]),
        event_bus=event_bus,
    )

    result = asyncio.run(agent.run())

    assert result.exit_requested is True
    assert agent.history == [
        {"role": "user", "content": "第一次"},
        {"role": "user", "content": "重试"},
        {"role": "assistant", "content": "重试成功"},
    ]


def test_request_input_snapshots_history_immediately_before_user_message() -> None:
    """交互输入路径应在追加本轮用户消息前保存历史浅快照。

    Returns:
        None。
    """
    event_bus = RecordingEventBus(["当前问题"])
    agent = _runtime_agent(SequenceLLM([]), event_bus=event_bus)
    original_messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧答案"},
    ]
    agent.history.extend(original_messages)
    ctx = RunContext(messages=agent.history)

    state = asyncio.run(agent._on_request_input(ctx))

    assert state is AgentState.CHECK_COMPACT
    assert ctx.turn_start_messages == original_messages
    assert ctx.turn_start_messages is not agent.history
    assert agent.history == [
        *original_messages,
        {"role": "user", "content": "当前问题"},
    ]


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), SystemExit(7)],
)
def test_control_flow_errors_propagate_unchanged(control_error: BaseException) -> None:
    """取消和进程退出等控制流异常不被 LLM 失败收口吞掉。"""
    agent = _runtime_agent(SequenceLLM([]))

    async def raising_handler(ctx: RunContext) -> AgentState:
        """抛出预设控制流异常。

        Args:
            ctx: 当前运行上下文。

        Returns:
            不返回。

        Raises:
            BaseException: 预设控制流异常。
        """
        del ctx
        raise control_error

    agent._handlers[AgentState.CHECK_COMPACT] = raising_handler
    ctx = RunContext(messages=[])

    with pytest.raises(type(control_error)) as raised:
        asyncio.run(agent._run_single_turn(ctx, AgentState.CHECK_COMPACT))

    assert raised.value is control_error


def test_run_result_exposes_optional_llm_error() -> None:
    """RunResult 默认无错误并能显式携带结构化 LLM 错误。"""
    info = _terminal_error(LLMErrorKind.BAD_REQUEST, "请求参数无效").info

    assert RunResult().llm_error is None
    assert RunResult(llm_error=info).llm_error is info


def test_llm_error_type_hints_are_resolvable_at_runtime() -> None:
    """RunContext 与 RunResult 的 llm_error 类型提示可在运行时解析。"""
    assert get_type_hints(RunContext)["llm_error"] == LLMErrorInfo | None
    assert get_type_hints(RunResult)["llm_error"] == LLMErrorInfo | None


def test_subagent_llm_failure_rolls_back_associated_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """子 agent 返回 llm_error 时返回安全文本并回滚关联任务。"""
    info = _terminal_error(LLMErrorKind.SERVICE, "子任务服务不可用").info

    class FakeChildAgent:
        """返回结构化 LLM 失败结果的子 agent。"""

        uuid = uuid.uuid4()
        history: list[dict] = []

        async def run(self, prompt: str) -> RunResult:
            """返回预设失败结果。

            Args:
                prompt: 子任务提示词。

            Returns:
                携带 llm_error 的安全结果。
            """
            del prompt
            return RunResult(final_text="service: 子任务服务不可用，请稍后重试。", llm_error=info)

    class RecordingTaskMgr:
        """记录任务状态更新。"""

        def __init__(self) -> None:
            self.updates: list[tuple[str, str, str]] = []

        def update(self, task_id: str, *, status: str, owner: str) -> None:
            """记录一次任务更新。

            Args:
                task_id: 任务 ID。
                status: 新状态。
                owner: 新负责人。

            Returns:
                None。
            """
            self.updates.append((task_id, status, owner))

    child = FakeChildAgent()
    monkeypatch.setattr(
        Agent,
        "from_manifest",
        classmethod(lambda cls, manifest, deps, **kwargs: child),
    )
    task_mgr = RecordingTaskMgr()
    parent = SimpleNamespace(
        _task_mgr=task_mgr,
        llm=SimpleNamespace(model="test"),
        enable_thinking=True,
        features=set(),
    )
    mgr = object.__new__(SubAgentMgr)
    mgr.workdir = tmp_path
    mgr.global_dir = None
    mgr._documents = {
        "worker": AgentManifest(
            agent_type="worker",
            description="test",
            path=tmp_path / "worker.md",
        )
    }
    mgr.deps = SimpleNamespace(
        tools_mgr=SimpleNamespace(resolve_subagent_tools=lambda tools: set()),
        hooks_mgr=None,
        event_bus=None,
    )

    text = asyncio.run(mgr.task_delegator("worker", "执行任务", parent_agent=parent, task_id="task-1"))

    assert text == "service: 子任务服务不可用，请稍后重试。"
    assert task_mgr.updates == [
        ("task-1", "in_progress", "worker"),
        ("task-1", "pending", ""),
    ]


class _SuccessfulChildAgent:
    """可成功返回或抛出预设异常的子 agent。"""

    def __init__(self, error: BaseException | None = None) -> None:
        """初始化子 agent。

        Args:
            error: run 时抛出的可选异常。

        Returns:
            None。
        """
        self.uuid = uuid.uuid4()
        self.history = [{"role": "user", "content": "child"}]
        self.error = error

    async def run(self, prompt: str) -> RunResult:
        """成功返回结果或抛出预设异常。

        Args:
            prompt: 子任务提示词。

        Returns:
            成功结果。

        Raises:
            BaseException: 配置了 error 时原样抛出。
        """
        del prompt
        if self.error is not None:
            raise self.error
        return RunResult(final_text="子任务成功")


class _RecordingTaskManager:
    """记录任务状态变更的测试 task manager。"""

    def __init__(self) -> None:
        """初始化空更新记录。

        Returns:
            None。
        """
        self.updates: list[tuple[str, str, str]] = []

    def update(self, task_id: str, *, status: str, owner: str) -> None:
        """记录任务更新。

        Args:
            task_id: 任务 ID。
            status: 新状态。
            owner: 新负责人。

        Returns:
            None。
        """
        self.updates.append((task_id, status, owner))


class _StageHooks:
    """可在指定子 agent hook 阶段抛出异常。"""

    def __init__(self, fail_event: str | None, error: BaseException) -> None:
        """初始化 hook 行为。

        Args:
            fail_event: 要失败的 hook 事件名。
            error: 失败时抛出的异常。

        Returns:
            None。
        """
        self.fail_event = fail_event
        self.error = error

    async def run_event(
        self,
        event: str,
        value: str,
        payload: dict,
        **kwargs: object,
    ) -> SimpleNamespace:
        """执行 hook 并在目标阶段抛出异常。

        Args:
            event: hook 事件名。
            value: hook 主值。
            payload: hook 载荷。
            **kwargs: hook 上下文。

        Returns:
            未阻断的 hook 结果。

        Raises:
            BaseException: 当前事件为失败阶段时原样抛出。
        """
        del value, payload, kwargs
        if event == self.fail_event:
            raise self.error
        return SimpleNamespace(blocked=False, block_reason=None, additional_context=[])


class _StageLifecycleBus:
    """记录 lifecycle phase 并可在 end 阶段失败。"""

    def __init__(self, end_error: BaseException | None = None) -> None:
        """初始化 lifecycle 总线。

        Args:
            end_error: end emit 时抛出的可选异常。

        Returns:
            None。
        """
        self.end_error = end_error
        self.phases: list[str] = []

    async def emit(self, event: object) -> None:
        """记录 phase 并按配置失败。

        Args:
            event: SubagentLifecycle 事件。

        Returns:
            None。

        Raises:
            BaseException: end 阶段配置了异常时原样抛出。
        """
        phase = str(getattr(event, "phase", ""))
        self.phases.append(phase)
        if phase == "end" and self.end_error is not None:
            raise self.end_error


class _SubagentTools:
    """返回空子 agent 工具集的测试 tools manager。"""

    def resolve_subagent_tools(self, tools: set[str] | None) -> set[str]:
        """返回空工具集。

        Args:
            tools: manifest 工具声明。

        Returns:
            空工具集。
        """
        del tools
        return set()


def _configured_subagent_mgr(
    tmp_path: Path,
    task_mgr: _RecordingTaskManager,
    hooks_mgr: _StageHooks,
    event_bus: _StageLifecycleBus,
) -> tuple[SubAgentMgr, SimpleNamespace]:
    """构造带关联任务的最小 SubAgentMgr 与父 agent。

    Args:
        tmp_path: 测试工作目录。
        task_mgr: 任务管理器。
        hooks_mgr: hook 管理器。
        event_bus: lifecycle 总线。

    Returns:
        SubAgentMgr 与父 agent。
    """
    parent = SimpleNamespace(
        _task_mgr=task_mgr,
        llm=SimpleNamespace(model="test"),
        enable_thinking=True,
        features=set(),
        uuid=uuid.uuid4(),
        agent_type="main",
    )
    mgr = object.__new__(SubAgentMgr)
    mgr.workdir = tmp_path
    mgr.global_dir = None
    mgr._documents = {
        "worker": AgentManifest(
            agent_type="worker",
            description="test",
            path=tmp_path / "worker.md",
        )
    }
    mgr.deps = SimpleNamespace(
        tools_mgr=_SubagentTools(),
        hooks_mgr=hooks_mgr,
        event_bus=event_bus,
        session_id="session",
    )
    return mgr, parent


@pytest.mark.parametrize("stage", ["construct", "start_hook", "end_emit", "stop_hook"])
def test_subagent_full_chain_failure_rolls_back_and_preserves_exception(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """构造、start/end lifecycle 与 stop hook 失败均回滚并传播原异常。"""
    primary: BaseException = SystemExit(7) if stage == "start_hook" else RuntimeError(stage)
    task_mgr = _RecordingTaskManager()
    hooks_mgr = _StageHooks(
        "SubagentStart" if stage == "start_hook" else "SubagentStop" if stage == "stop_hook" else None,
        primary,
    )
    event_bus = _StageLifecycleBus(primary if stage == "end_emit" else None)
    mgr, parent = _configured_subagent_mgr(tmp_path, task_mgr, hooks_mgr, event_bus)
    child = _SuccessfulChildAgent()

    def from_manifest(
        cls: type[Agent],
        manifest: AgentManifest | None,
        deps: object,
        **overrides: object,
    ) -> _SuccessfulChildAgent:
        """返回测试子 agent 或在构造阶段抛错。

        Args:
            cls: Agent 类。
            manifest: 子 agent manifest。
            deps: Agent 依赖。
            **overrides: 构造覆盖字段。

        Returns:
            测试子 agent。

        Raises:
            BaseException: 构造失败场景的原异常。
        """
        del cls, manifest, deps, overrides
        if stage == "construct":
            raise primary
        return child

    monkeypatch.setattr(Agent, "from_manifest", classmethod(from_manifest))

    with pytest.raises(type(primary)) as raised:
        asyncio.run(mgr.task_delegator("worker", "执行", parent_agent=parent, task_id="task-1"))

    assert raised.value is primary
    assert task_mgr.updates == [
        ("task-1", "in_progress", "worker"),
        ("task-1", "pending", ""),
    ]
    if stage != "construct":
        assert "end" in event_bus.phases


def test_subagent_primary_control_flow_error_survives_end_emit_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """agent.run 的控制流异常不被 lifecycle end emit 的次级异常覆盖。"""
    primary = asyncio.CancelledError("primary")
    secondary = RuntimeError("secondary end failure")
    task_mgr = _RecordingTaskManager()
    hooks_mgr = _StageHooks(None, RuntimeError("unused"))
    event_bus = _StageLifecycleBus(secondary)
    mgr, parent = _configured_subagent_mgr(tmp_path, task_mgr, hooks_mgr, event_bus)
    child = _SuccessfulChildAgent(primary)

    def from_manifest(
        cls: type[Agent],
        manifest: AgentManifest | None,
        deps: object,
        **overrides: object,
    ) -> _SuccessfulChildAgent:
        """返回会抛控制流异常的测试子 agent。

        Args:
            cls: Agent 类。
            manifest: 子 agent manifest。
            deps: Agent 依赖。
            **overrides: 构造覆盖字段。

        Returns:
            测试子 agent。
        """
        del cls, manifest, deps, overrides
        return child

    monkeypatch.setattr(Agent, "from_manifest", classmethod(from_manifest))

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(mgr.task_delegator("worker", "执行", parent_agent=parent, task_id="task-1"))

    assert raised.value is primary
    assert event_bus.phases == ["start", "end"]
    assert task_mgr.updates == [
        ("task-1", "in_progress", "worker"),
        ("task-1", "pending", ""),
    ]


@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        (
            LLMErrorKind.AUTHENTICATION,
            "认证配置无效",
            "错误：LLM 调用失败（authentication）：认证配置无效。请检查 API 凭据和模型配置后重试。",
        ),
        (
            LLMErrorKind.SERVICE,
            "服务暂时不可用，请稍后重试。",
            "错误：LLM 调用失败（service）：服务暂时不可用，请稍后重试。",
        ),
        (
            LLMErrorKind.CONTENT_POLICY,
            "请求被内容策略拦截。",
            "错误：LLM 调用失败（content_policy）：请求被内容策略拦截。请调整请求内容后重试。",
        ),
        (
            LLMErrorKind.OUTPUT_LIMIT,
            "输出过长，请缩小输出范围后重试。",
            "错误：LLM 调用失败（output_limit）：输出过长，请缩小输出范围后重试。",
        ),
    ],
)
def test_llm_failure_text_uses_kind_specific_non_repeated_advice(
    kind: LLMErrorKind,
    message: str,
    expected: str,
) -> None:
    """普通失败文案按 kind 给出建议且不产生双句号或重复建议。"""
    agent = _runtime_agent(SequenceLLM([]))
    ctx = RunContext(
        messages=[],
        llm_error=LLMErrorInfo(kind=kind, message=message, retryable=False),
    )

    state = asyncio.run(agent._on_llm_failure(ctx))

    assert state is AgentState.DONE
    assert ctx.final_text == expected
