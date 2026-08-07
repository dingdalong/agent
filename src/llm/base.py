"""LLM Provider 抽象基类与结构化输出支持。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Collection,
    Mapping,
    Optional,
)
import asyncio
from contextvars import ContextVar
import logging
import math
import time
import uuid
import openai
from src.events import EventBus, emit_telemetry_safely
from src.events.types import (
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    LLMRetrying,
    ResponseDelta,
    ThinkingDelta,
)
from src.llm.errors import (
    LLMCallError,
    LLMErrorInfo,
    LLMErrorKind,
    LLMStreamResponseError,
    classify_llm_error,
    safe_exception_traceback,
)
from src.llm.retry import RetryConfig, RetryPolicy
from src.web.types import (
    NativeWebCapabilityError,
    WebFetchResponse,
    WebSearchResponse,
)

if TYPE_CHECKING:
    from src.tools import ToolDict

logger = logging.getLogger(__name__)


# 合法推理力度档位（各 provider 降档阶梯 _EFFORT_DOWNGRADE 的并集）。
VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def normalize_reasoning_effort(text: str) -> str | None:
    """规整推理力度档位。

    Args:
        text: 原始档位文本。

    Returns:
        小写去空白后命中合法档位时返回规范值，否则返回 None。
    """
    value = text.strip().lower()
    return value if value in VALID_REASONING_EFFORTS else None


@dataclass(slots=True)
class LLMCallContext:
    """单次 chat 尝试独占的流式输出与调用方状态。"""

    attempt: int
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    response_displayed: bool = False
    thinking_displayed: bool = False
    tool_fragment_state: str = "none"
    response_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_fragments: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def partial_output(self) -> str:
        """返回当前尝试已接收的正文片段。

        Returns:
            按到达顺序拼接的正文。
        """
        return "".join(self.response_parts)

    @property
    def partial_thinking(self) -> str:
        """返回当前尝试已接收的思考片段。

        Returns:
            按到达顺序拼接的思考文本。
        """
        return "".join(self.thinking_parts)

    @property
    def has_partial_data(self) -> bool:
        """返回当前尝试是否已收到任何流式残片。

        Returns:
            已收到正文、思考或工具片段时为 True。
        """
        return bool(
            self.response_parts
            or self.thinking_parts
            or self.tool_fragment_state != "none"
        )

    def record_response_delta(self, content: str) -> None:
        """记录当前尝试的正文增量。

        Args:
            content: 新收到的正文片段。

        Returns:
            None。
        """
        if not content:
            return
        self.response_parts.append(content)
        self.response_displayed = True

    def record_thinking_delta(self, content: str) -> None:
        """记录当前尝试的思考增量。

        Args:
            content: 新收到的思考片段。

        Returns:
            None。
        """
        if not content:
            return
        self.thinking_parts.append(content)
        self.thinking_displayed = True

    def record_tool_fragment(
        self,
        index: int,
        *,
        call_id: str = "",
        name: str = "",
        arguments: str = "",
        complete: bool = False,
    ) -> None:
        """记录当前尝试的工具调用流片段状态。

        Args:
            index: provider 流中的工具调用索引。
            call_id: 本片段携带的调用 ID。
            name: 本片段携带的工具名。
            arguments: 本片段携带的参数文本。
            complete: 工具片段序列是否已完整结束。

        Returns:
            None。
        """
        fragment = self.tool_fragments.setdefault(
            index,
            {"id": "", "name": "", "arguments": ""},
        )
        if call_id:
            fragment["id"] = call_id
        fragment["name"] += name
        fragment["arguments"] += arguments
        self.tool_fragment_state = "complete" if complete else "partial"

    def mark_tool_fragments_complete(self) -> None:
        """把已收到的工具片段序列标记为完整。

        Returns:
            None。
        """
        if self.tool_fragments:
            self.tool_fragment_state = "complete"


_ACTIVE_LLM_CALL: ContextVar[LLMCallContext | None] = ContextVar(
    "active_llm_call",
    default=None,
)

class TruncationKind(str, Enum):
    """finish_reason == "length" 时响应被截断所处阶段的分类。"""
    TOOL_CALL = "tool_call"
    CONTENT = "content"
    THINKING = "thinking"
    UNKNOWN = "unknown"


@dataclass
class LLMResponse:
    """LLM 响应。"""
    content: str
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    assistant_message: Optional[dict] = None
    token_usage: dict[str, int | None] | None = None
    has_partial_data: bool = False
    truncation_kind: str | None = None
    call_id: str = ""


def _has_reasoning_carrier(assistant_message: dict | None) -> bool:
    """判断 assistant 消息是否携带任意 provider 的推理载体。

    Args:
        assistant_message: provider 归一化后的 assistant 消息，可能为 None。

    Returns:
        含 reasoning_content / reasoning 文本、Anthropic thinking 块或
        Responses API reasoning 项时为 True。
    """
    if not isinstance(assistant_message, dict):
        return False
    if assistant_message.get("reasoning_content") or assistant_message.get("reasoning"):
        return True
    anthropic_content = assistant_message.get("_anthropic_content")
    if isinstance(anthropic_content, list) and any(
        isinstance(block, dict) and block.get("type") == "thinking"
        for block in anthropic_content
    ):
        return True
    response_output = assistant_message.get("_response_output")
    if isinstance(response_output, list) and any(
        isinstance(item, dict) and item.get("type") == "reasoning"
        for item in response_output
    ):
        return True
    return False


def classify_truncation(
    response: LLMResponse,
    call: LLMCallContext | None = None,
) -> str:
    """按残片所处阶段将长度截断分类为四类之一。

    Args:
        response: finish_reason 为 length 的成品响应。
        call: 可选的调用上下文，提供流式残片作兜底信号。

    Returns:
        TruncationKind 之一的字符串值；优先级为工具 → 正文 → 思考 → 未知。
    """
    assistant_message = response.assistant_message or {}
    has_tool = bool(
        response.tool_calls
        or assistant_message.get("tool_calls")
        or (call is not None and call.tool_fragment_state != "none")
    )
    if has_tool:
        return TruncationKind.TOOL_CALL.value
    has_content = bool(
        (response.content or "").strip()
        or (call is not None and call.response_parts)
    )
    if has_content:
        return TruncationKind.CONTENT.value
    has_thinking = _has_reasoning_carrier(assistant_message) or bool(
        call is not None and call.thinking_parts
    )
    if has_thinking:
        return TruncationKind.THINKING.value
    return TruncationKind.UNKNOWN.value


def validate_chat_completion_stream(
    finish_reason: str | None,
    tool_calls: Mapping[int, Mapping[str, str]],
    *,
    valid_finish_reasons: Collection[str],
) -> None:
    """校验 Chat Completions 流终态与完整工具调用。

    Args:
        finish_reason: 流末尾收到的终止原因。
        tool_calls: 按流索引合并后的工具调用。
        valid_finish_reasons: 当前 provider 允许正常返回的终止原因集合。

    Returns:
        None。

    Raises:
        LLMStreamResponseError: 终态缺失、非法、触发内容政策或工具调用畸形。
    """
    if finish_reason is None:
        raise LLMStreamResponseError(
            "流式响应在合法终态前结束",
            code="invalid_response",
        )
    if finish_reason == "content_filter":
        raise LLMStreamResponseError(
            "响应被内容政策过滤",
            code="content_filter",
        )
    if finish_reason not in valid_finish_reasons:
        raise LLMStreamResponseError(
            "流式响应包含未知 finish_reason",
            code="invalid_response",
        )
    if finish_reason == "length":
        return

    if finish_reason == "tool_calls" and not tool_calls:
        raise LLMStreamResponseError(
            "tool_calls 终态未包含工具调用",
            code="invalid_response",
        )
    if finish_reason == "stop" and tool_calls:
        raise LLMStreamResponseError(
            "stop 终态不得包含工具调用",
            code="invalid_response",
        )

    validate_tool_calls(tool_calls)


def validate_tool_calls(
    tool_calls: Mapping[int, Mapping[str, str]],
) -> None:
    """校验完整工具调用的字段、JSON object 参数与 ID 唯一性。

    Args:
        tool_calls: 按流索引合并后的完整工具调用。

    Returns:
        None。

    Raises:
        LLMStreamResponseError: 任一工具调用字段、参数或 ID 唯一性非法。
    """
    call_ids: set[str] = set()

    for tool_call in tool_calls.values():
        call_id = tool_call.get("id")
        name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        if not isinstance(call_id, str) or not call_id.strip():
            raise LLMStreamResponseError(
                "工具调用 ID 为空或类型非法",
                code="invalid_response",
            )
        if call_id in call_ids:
            raise LLMStreamResponseError(
                "工具调用 ID 重复",
                code="invalid_response",
            )
        call_ids.add(call_id)
        if not isinstance(name, str) or not name.strip():
            raise LLMStreamResponseError(
                "工具调用名称为空或类型非法",
                code="invalid_response",
            )
        if not isinstance(arguments, str):
            raise LLMStreamResponseError(
                "工具调用参数不是 JSON 文本",
                code="invalid_response",
            )
        try:
            parsed_arguments = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LLMStreamResponseError(
                "工具调用参数不是合法 JSON",
                code="invalid_response",
            ) from exc
        if not isinstance(parsed_arguments, dict):
            raise LLMStreamResponseError(
                "工具调用参数必须是 JSON object",
                code="invalid_response",
            )


async def iter_llm_stream(stream: AsyncIterable[Any]) -> AsyncIterator[Any]:
    """迭代 provider 流并把显式半截 EOF 转为响应协议错误。

    Args:
        stream: provider SDK 返回的异步事件流。

    Yields:
        provider 流中的下一个事件或数据块。

    Raises:
        LLMStreamResponseError: 流在数据帧中途抛出 EOFError。
        BaseException: 其他 SDK、网络或控制流异常原样传播。
    """
    try:
        async for event in stream:
            yield event
    except EOFError as exc:
        raise LLMStreamResponseError(
            "流式响应在数据帧中途结束",
            code="invalid_response",
        ) from exc

@dataclass
class LLMProvider(ABC):
    """所有 LLM 实现的抽象基类。"""
    api_key: str
    base_url: str
    model: str
    event_bus: EventBus
    concurrency: int = 5
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    timeout: float = 120.0
    context_limit: int = 0
    page_token_rate: float = 0.03
    page_token_budget: int = field(init=False)
    supports_native_structured_output: bool = False
    reasoning_effort: str = "max"
    preserve_thinking: bool = False
    user_agent: str = ""
    max_pause_turn_continuations: int = 0

    # 推理力度降档阶梯（当前档 → 下一更低档），各 provider 覆写；空表示无阶梯。
    _EFFORT_DOWNGRADE: ClassVar[dict[str, str]] = {}

    def __post_init__(self) -> None:
        """初始化并发限制、分页预算与统一重试策略。

        Returns:
            None。
        """
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.page_token_budget = max(1, math.floor(self.context_limit * self.page_token_rate))
        self._retry_policy = RetryPolicy(RetryConfig(
            max_attempts=self.max_attempts,
            base_delay_seconds=self.base_delay_seconds,
            max_delay_seconds=self.max_delay_seconds,
        ))

    def next_lower_effort(self, current: str) -> str | None:
        """查询给定推理力度的下一个更低档位。

        Args:
            current: 当前推理力度档位名。

        Returns:
            存在更低档位时返回其名称，触底或该 provider 无阶梯时返回 None。
        """
        return self._EFFORT_DOWNGRADE.get(current)

    def protocol_continuation_limit(self, finish_reason: str) -> int:
        """查询指定协议终态允许的自动续接次数。

        Args:
            finish_reason: provider 归一化后的终态原因。

        Returns:
            非协议续接 provider 固定返回 0。
        """
        return 0

    async def native_web_search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> WebSearchResponse:
        """执行 provider 原生搜索；不支持时只抛能力异常供本地回退。"""
        raise NativeWebCapabilityError(f"{type(self).__name__} 不支持原生 Web 搜索")

    async def native_web_fetch(self, url: str) -> WebFetchResponse:
        """执行 provider 原生抓取；不支持时只抛能力异常供本地回退。"""
        raise NativeWebCapabilityError(f"{type(self).__name__} 不支持原生 Web 抓取")

    async def _run_auxiliary(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """为不进入主对话的 provider 辅助调用复用并发、分类与重试策略。"""
        async with self._semaphore:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    return await operation()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    info = classify_llm_error(exc)
                    diagnostic_id = f"llm_{uuid.uuid4().hex[:12]}"
                    self._log_llm_failure(
                        info=info,
                        attempt=attempt,
                        diagnostic_id=diagnostic_id,
                        exc=exc,
                    )
                    if not self._retry_policy.should_retry(info, attempt):
                        raise LLMCallError(
                            info=info,
                            attempts=attempt,
                            diagnostic_id=diagnostic_id,
                        ) from exc
                    await self._sleep(self._retry_policy.delay(info, attempt=attempt))
        raise AssertionError("辅助调用重试循环应成功返回或抛出终态异常")

    @staticmethod
    def _ua_headers(user_agent: str) -> dict[str, str] | None:
        """构造传给底层 SDK 客户端的自定义请求头。

        Args:
            user_agent: 自定义 User-Agent 字符串。

        Returns:
            user_agent 非空时返回 {"User-Agent": user_agent}，否则返回 None（沿用 SDK 默认 UA）。
        """
        return {"User-Agent": user_agent} if user_agent else None

    def clear_reasoning_content(self, message): ...

    @classmethod
    async def list_models(
        cls,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        user_agent: str = "",
    ) -> list[str]:
        """从 OpenAI 兼容 Models API 获取模型列表。

        Args:
            api_key: provider API 密钥。
            base_url: provider API 根地址。
            timeout: SDK 请求与外层等待的超时秒数。
            user_agent: 可选自定义 User-Agent。

        Returns:
            provider 返回的模型 ID 列表。
        """
        client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0,
            default_headers=cls._ua_headers(user_agent),
        )
        try:
            response = await asyncio.wait_for(client.models.list(), timeout=timeout)
            return [m.id for m in response.data]
        finally:
            await client.close()

    @abstractmethod
    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        """Estimate tokens in the request payload actually sent by this provider.

        Args:
            messages: Conversation messages to include after provider conversion.
            prompt: Optional system prompt to include after provider conversion.
            tools: Optional tool schemas to include after provider conversion.

        Returns:
            Estimated token count for the complete provider-specific input payload.
        """
        ...

    def _split_page_once(self, text: str) -> tuple[str, str]:
        if self.estimate_tokens([{"role": "tool", "content": text}]) <= self.page_token_budget:
            return text, ""

        lo = 0
        hi = len(text)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.estimate_tokens([{"role": "tool", "content": text[:mid]}]) <= self.page_token_budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best <= 0:
            best = 1
        return text[:best], text[best:]

    def split_page(self, text: str) -> list[str]:
        pages: list[str] = []
        remaining = text
        while remaining:
            page, remaining = self._split_page_once(remaining)
            pages.append(page)
        return pages or [""]

    async def _sleep(self, delay: float) -> None:
        """等待指定秒数后继续重试。

        Args:
            delay: 等待秒数。

        Returns:
            None。
        """
        await asyncio.sleep(delay)

    def normalize_messages(
        self,
        messages: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False,
    ) -> list[dict]:
        """把消息清洗为 provider 可安全接收的通用序列。

        Args:
            messages: 单条消息字典或消息字典列表。
            allow_developer_role: 是否允许 developer 角色保留在结果中。
            allow_tool_calls: 是否保留 assistant/tool 的工具调用协议字段。
            strict: 是否在发现非法消息或工具调用序列时抛出异常。

        Returns:
            字段已规范化、工具调用与响应严格配对的消息列表。

        Raises:
            TypeError: messages 类型非法，或严格模式下消息元素类型非法。
            ValueError: 严格模式下消息字段或工具调用序列非法。
        """
        VALID_ROLES = {"system", "user", "assistant", "tool"}
        if allow_developer_role:
            VALID_ROLES.add("developer")

        if isinstance(messages, dict):
            raw_messages = [messages]
        elif isinstance(messages, list):
            raw_messages = list(messages)
        else:
            raise TypeError(
                f"messages 必须是 dict 或 list[dict]，当前类型: {type(messages).__name__}"
            )

        normalized: list[dict] = []

        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                if strict:
                    raise TypeError(f"messages[{idx}] 必须是 dict，当前类型: {type(msg).__name__}")
                continue

            role = msg.get("role", "").strip().lower()
            if not role:
                if strict:
                    raise ValueError(f"messages[{idx}] 缺少必填字段 'role'")
                role = "user"

            role = self._normalize_role(role)

            if role not in VALID_ROLES:
                if strict:
                    raise ValueError(
                        f"messages[{idx}] role='{role}' 不被支持。"
                        f"支持的 role: {sorted(VALID_ROLES)}"
                    )
                role = "user"

            content = self._normalize_content(msg.get("content", ""))
            has_tool_calls = bool(msg.get("tool_calls"))
            norm_msg: dict = {"role": role, "content": content}

            if role == "assistant" and has_tool_calls and allow_tool_calls:
                tool_calls = self._normalize_tool_calls(msg.get("tool_calls"))
                # 空列表仅作为序列校验的内部非法标记，最终不会进入返回值。
                norm_msg["tool_calls"] = tool_calls or []

            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                if not tool_call_id and strict:
                    raise ValueError(f"messages[{idx}] role='tool' 但缺少 tool_call_id")
                norm_msg["tool_call_id"] = tool_call_id
                if not allow_tool_calls:
                    norm_msg["role"] = "user"
                    norm_msg.pop("tool_call_id", None)

            standard_keys = set(norm_msg)
            self._normalize_assistant_extra(msg, norm_msg, role)
            has_provider_extra = bool(set(norm_msg) - standard_keys)
            if (
                not content
                and not has_tool_calls
                and not has_provider_extra
                and role != "tool"
            ):
                continue

            if "name" in msg and isinstance(msg["name"], str):
                norm_msg["name"] = msg["name"]

            normalized.append(norm_msg)

        if not allow_tool_calls:
            return normalized
        return self._normalize_tool_message_sequence(normalized, strict)

    def _normalize_tool_message_sequence(
        self,
        messages: list[dict],
        strict: bool,
    ) -> list[dict]:
        """校验并修复 assistant 工具调用与紧随其后的 tool 响应。

        Args:
            messages: 已完成单条字段规范化的消息列表。
            strict: 非法工具序列是否直接抛出 ValueError。

        Returns:
            仅包含完整工具往返的消息列表；默认模式会删除非法工具载体。

        Raises:
            ValueError: strict 为 True 且消息中存在非法工具调用序列。
        """
        repaired: list[dict] = []
        idx = 0

        while idx < len(messages):
            message = messages[idx]
            if message.get("role") == "assistant" and "tool_calls" in message:
                end_idx = idx + 1
                while (
                    end_idx < len(messages)
                    and messages[end_idx].get("role") == "tool"
                ):
                    end_idx += 1

                tool_messages = messages[idx + 1:end_idx]
                error = self._tool_message_sequence_error(message, tool_messages)
                if error is None:
                    repaired.extend(messages[idx:end_idx])
                elif strict:
                    raise ValueError(f"messages[{idx}] 工具调用序列非法：{error}")
                else:
                    logger.warning(
                        "删除非法工具调用消息组: assistant_index=%d, "
                        "tool_message_count=%d, reason=%s",
                        idx,
                        len(tool_messages),
                        error,
                    )
                    content = message.get("content")
                    if content:
                        repaired.append({"role": "assistant", "content": content})
                idx = end_idx
                continue

            if message.get("role") == "tool":
                end_idx = idx + 1
                while (
                    end_idx < len(messages)
                    and messages[end_idx].get("role") == "tool"
                ):
                    end_idx += 1
                orphan_count = end_idx - idx
                if strict:
                    raise ValueError(f"messages[{idx}] 存在游离的 tool 消息")
                logger.warning(
                    "删除游离的 tool 消息: start_index=%d, tool_message_count=%d",
                    idx,
                    orphan_count,
                )
                idx = end_idx
                continue

            repaired.append(message)
            idx += 1

        return repaired

    def _tool_message_sequence_error(
        self,
        assistant_message: dict,
        tool_messages: list[dict],
    ) -> str | None:
        """返回单个工具调用消息组的结构错误。

        Args:
            assistant_message: 携带 tool_calls 的 assistant 消息。
            tool_messages: 紧随 assistant 的连续 tool 消息。

        Returns:
            序列合法时返回 None，否则返回不含工具参数内容的错误说明。
        """
        tool_calls = assistant_message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return "assistant 未包含有效工具调用"

        call_ids = [
            call.get("id") if isinstance(call, dict) else None
            for call in tool_calls
        ]
        if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
            return "assistant 工具调用 ID 为空或类型非法"
        if len(set(call_ids)) != len(call_ids):
            return "assistant 工具调用 ID 重复"

        response_ids = [message.get("tool_call_id") for message in tool_messages]
        if any(
            not isinstance(response_id, str) or not response_id
            for response_id in response_ids
        ):
            return "tool 消息的工具调用 ID 为空或类型非法"
        if len(set(response_ids)) != len(response_ids):
            return "同一工具调用存在重复 tool 响应"
        if set(response_ids) != set(call_ids):
            return "工具调用与紧随其后的 tool 响应不完整或不匹配"
        return None

    def _normalize_role(self, role: str) -> str:
        return role

    def _normalize_content(self, content) -> str | list[dict] | None:
        if isinstance(content, list):
            return [p for p in content if isinstance(p, dict)] or ""
        if content is not None and not isinstance(content, str):
            return str(content)
        return content

    def _normalize_tool_calls(self, tool_calls) -> list[dict] | None:
        if not tool_calls or not isinstance(tool_calls, list):
            return None
        valid_calls = []
        for call in tool_calls:
            if isinstance(call, dict) and "function" in call:
                valid_calls.append({
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": call["function"],
                })
        return valid_calls or None

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        pass

    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        ephemeral_instruction: str | None = None,
    ) -> LLMResponse:
        """执行带统一分类、退避和尝试级隔离的 LLM 调用。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词消息列表。
            tools: 可选工具 schema 列表。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            caller_agent_type: 发起调用的 agent 类型。
            caller_uuid: 发起调用的 agent 实例 UUID。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位；
                None 时沿用 provider 的 reasoning_effort，不修改共享实例。
            ephemeral_instruction: 一次性追加到消息尾部的 user 指令；
                仅作用于本次调用，不写回调用方 messages。

        Returns:
            最终成功尝试产生的 LLM 响应。

        Raises:
            LLMCallError: 不可重试或重试耗尽时的结构化终态错误。
            asyncio.CancelledError: 调用被取消时原样传播。
            KeyboardInterrupt: 收到键盘中断时原样传播。
            SystemExit: 进程退出时原样传播。
        """
        effective_messages = (
            [*messages, {"role": "user", "content": ephemeral_instruction}]
            if ephemeral_instruction
            else messages
        )
        async with self._semaphore:
            for attempt in range(1, self.max_attempts + 1):
                call = LLMCallContext(
                    attempt=attempt,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )
                try:
                    started_at = await self._emit_llm_call_started(
                        effective_messages,
                        prompt,
                        tools,
                        call,
                    )
                    call_token = _ACTIVE_LLM_CALL.set(call)
                    try:
                        response = await self._do_chat(
                            messages=effective_messages,
                            prompt=prompt,
                            tools=tools,
                            temperature=temperature,
                            tool_choice=tool_choice,
                            enable_thinking=enable_thinking,
                            reasoning_effort_override=reasoning_effort_override,
                            call=call,
                        )
                        response.has_partial_data = call.has_partial_data
                        response.call_id = call.call_id
                        if response.finish_reason == "length":
                            response.truncation_kind = classify_truncation(response, call)
                    finally:
                        _ACTIVE_LLM_CALL.reset(call_token)
                    if response.finish_reason != "length":
                        for index, tool_call in response.tool_calls.items():
                            if index not in call.tool_fragments:
                                call.record_tool_fragment(
                                    index,
                                    call_id=tool_call.get("id", ""),
                                    name=tool_call.get("name", ""),
                                    arguments=tool_call.get("arguments", ""),
                                    complete=True,
                                )
                        call.mark_tool_fragments_complete()
                    await self._emit_llm_call_completed(
                        started_at=started_at,
                        usage=response.token_usage,
                        call=call,
                    )
                    return response
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    info = classify_llm_error(exc)
                    diagnostic_id = f"llm_{uuid.uuid4().hex[:12]}"
                    self._log_llm_failure(
                        info=info,
                        attempt=attempt,
                        diagnostic_id=diagnostic_id,
                        exc=exc,
                    )
                    if not self._retry_policy.should_retry(info, attempt):
                        terminal = LLMCallError(
                            info=info,
                            attempts=attempt,
                            partial_output=call.partial_output,
                            diagnostic_id=diagnostic_id,
                        )
                        await self._emit_llm_call_failed(terminal=terminal, call=call)
                        raise terminal from exc
                    wait_time = self._retry_policy.delay(info, attempt=attempt)
                    await self._emit_llm_retrying(
                        info=info,
                        call=call,
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                        wait_seconds=wait_time,
                    )
                    await self._sleep(wait_time)
            raise AssertionError("重试循环应在成功返回或终态异常处结束")

    def _log_llm_failure(
        self,
        *,
        info: LLMErrorInfo,
        attempt: int,
        diagnostic_id: str,
        exc: Exception,
    ) -> None:
        """记录不含请求体、响应体和凭据的失败诊断。

        Args:
            info: 已安全化的结构化错误信息。
            attempt: 已失败的 1 基尝试序号。
            diagnostic_id: 本次失败的日志关联 ID。
            exc: 原始底层异常，仅未知类别保留安全化堆栈。

        Returns:
            None。
        """
        fields = (
            "LLM 调用失败 kind=%s retryable=%s status=%s provider_code=%s "
            "request_id=%s exception_type=%s attempt=%d/%d diagnostic_id=%s message=%r"
        )
        args = (
            info.kind.value,
            info.retryable,
            info.status_code,
            info.provider_code,
            info.request_id,
            info.original_exception_type,
            attempt,
            self.max_attempts,
            diagnostic_id,
            info.message,
        )
        if info.kind is not LLMErrorKind.UNKNOWN:
            logger.warning(fields, *args)
            return
        traceback = safe_exception_traceback(exc)
        safe_exception = RuntimeError(info.message).with_traceback(traceback)
        logger.error(
            fields,
            *args,
            exc_info=(type(safe_exception), safe_exception, traceback),
        )

    async def emit_response_delta(
        self,
        content: str,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        *,
        call: LLMCallContext | None = None,
    ) -> None:
        """记录并发出正文流增量。

        Args:
            content: 新收到的正文片段。
            caller_agent_type: 兼容调用方 agent 类型；call 存在时忽略。
            caller_uuid: 兼容调用方实例 UUID；call 存在时忽略。
            call: 当前独立调用尝试上下文。

        Returns:
            None。
        """
        call = call or _ACTIVE_LLM_CALL.get()
        if call is not None:
            call.record_response_delta(content)
            caller_agent_type = call.caller_agent_type
            caller_uuid = call.caller_uuid
        await emit_telemetry_safely(self.event_bus, ResponseDelta(
            timestamp=time.time(),
            source=self.model,
            content=content,
            call_id=call.call_id if call is not None else "",
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def emit_thinking_delta(
        self,
        content: str,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
        *,
        call: LLMCallContext | None = None,
    ) -> None:
        """记录并发出思考流增量。

        Args:
            content: 新收到的思考片段。
            caller_agent_type: 兼容调用方 agent 类型；call 存在时忽略。
            caller_uuid: 兼容调用方实例 UUID；call 存在时忽略。
            call: 当前独立调用尝试上下文。

        Returns:
            None。
        """
        call = call or _ACTIVE_LLM_CALL.get()
        if call is not None:
            call.record_thinking_delta(content)
            caller_agent_type = call.caller_agent_type
            caller_uuid = call.caller_uuid
        await emit_telemetry_safely(self.event_bus, ThinkingDelta(
            timestamp=time.time(),
            source=self.model,
            content=content,
            call_id=call.call_id if call is not None else "",
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        ))

    async def _emit_llm_retrying(
        self,
        info: LLMErrorInfo,
        call: LLMCallContext,
        attempt: int,
        max_attempts: int,
        wait_seconds: float,
    ) -> None:
        """发出 LLMRetrying 事件供状态条实时倒计时。

        Args:
            info: 触发重试的安全结构化错误信息。
            call: 失败尝试的独立调用上下文。
            attempt: 已失败的尝试序号（1 基）。
            max_attempts: 允许的最大尝试次数。
            wait_seconds: 本次等待秒数（含抖动的原始浮点值）。
        """
        logger.debug(
            "API错误 (%s)，%.1f秒后重试 (%d/%d)...",
            info.kind.value, wait_seconds, attempt, max_attempts,
        )
        await emit_telemetry_safely(self.event_bus, LLMRetrying(
            timestamp=time.time(),
            source=self.model,
            error_kind=info.kind.value,
            call_id=call.call_id,
            safe_message=info.message,
            partial=call.has_partial_data,
            tool_fragment_state=call.tool_fragment_state,
            attempt=attempt,
            max_attempts=max_attempts,
            wait_seconds=wait_seconds,
            caller_agent_type=call.caller_agent_type,
            caller_uuid=call.caller_uuid,
        ))

    async def _emit_llm_call_failed(
        self,
        terminal: LLMCallError,
        call: LLMCallContext,
    ) -> None:
        """发出一次不含原始异常或请求响应内容的 LLM 终态失败事件。

        Args:
            terminal: 即将抛给调用方的结构化终态错误。
            call: 最后一轮失败尝试的独立调用上下文。

        Returns:
            None。
        """
        info = terminal.info
        await emit_telemetry_safely(self.event_bus, LLMCallFailed(
            timestamp=time.time(),
            source=self.model,
            error_kind=info.kind.value,
            call_id=call.call_id,
            safe_message=info.message,
            attempts=terminal.attempts,
            partial=call.has_partial_data,
            tool_fragment_state=call.tool_fragment_state,
            status_code=info.status_code,
            provider_code=info.provider_code,
            request_id=info.request_id,
            diagnostic_id=terminal.diagnostic_id,
            caller_agent_type=call.caller_agent_type,
            caller_uuid=call.caller_uuid,
        ))

    async def _emit_llm_call_started(
        self,
        messages: list[dict],
        prompt: list[dict] | None,
        tools: list[ToolDict] | None,
        call: LLMCallContext,
    ) -> float:
        """发出 LLMCallStarted 事件并返回起始时间戳。

        Args:
            messages: 本次提交的消息列表。
            prompt: 系统提示词消息列表（可为 None）。
            tools: 本次可用工具列表（可为 None）。
            call: 当前尝试序号与调用方身份。
        Returns:
            本次调用的起始时间戳（time.time()），供完成事件计算耗时。
        """
        started_at = time.time()
        all_messages = (prompt or []) + messages
        estimated_input_tokens = await asyncio.to_thread(
            self.estimate_tokens,
            messages,
            prompt,
            tools,
        )
        await emit_telemetry_safely(self.event_bus, LLMCallStarted(
            timestamp=started_at,
            source=self.model,
            model=self.model,
            call_id=call.call_id,
            context_limit=self.context_limit,
            estimated_input_tokens=estimated_input_tokens,
            message_count=len(all_messages),
            tool_count=len(tools or []),
            attempt=call.attempt,
            max_attempts=self.max_attempts,
            caller_agent_type=call.caller_agent_type,
            caller_uuid=call.caller_uuid,
        ))
        return started_at

    async def _emit_llm_call_completed(
        self,
        started_at: float | None,
        ended_at: float | None = None,
        usage: dict[str, int | None] | None = None,
        call: LLMCallContext | None = None,
    ) -> None:
        """发出 LLMCallCompleted 事件。

        Args:
            started_at: 调用起始时间戳。
            ended_at: 调用结束时间戳（None 时用当前时间）。
            usage: token 用量字典。
            caller_uuid: 发起本次调用的 agent 实例 uuid，供路由器按 agent 累计 token。
        """
        if started_at is None:
            return

        completed_at = ended_at if ended_at is not None else time.time()
        duration = max(completed_at - started_at, 0.0)
        usage = usage or {}
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")

        await emit_telemetry_safely(self.event_bus, LLMCallCompleted(
            timestamp=completed_at,
            source=self.model,
            model=self.model,
            call_id=call.call_id if call is not None else "",
            input_tokens=usage.get("input_tokens"),
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_input_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
            duration_seconds=duration,
            output_tokens_per_second=output_tokens / duration if output_tokens is not None and duration > 0 else None,
            total_tokens_per_second=total_tokens / duration if total_tokens is not None and duration > 0 else None,
            caller_agent_type=call.caller_agent_type if call is not None else None,
            caller_uuid=call.caller_uuid if call is not None else None,
        ))

    @abstractmethod
    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | dict | None = None,
        enable_thinking: bool = True,
        reasoning_effort_override: str | None = None,
        *,
        call: LLMCallContext,
    ) -> LLMResponse:
        """执行单次 provider 调用且不在内部自动重试。

        Args:
            messages: 会话消息列表。
            prompt: 可选系统提示词列表。
            tools: 可选工具 schema 列表。
            temperature: 采样温度。
            tool_choice: 工具选择策略。
            enable_thinking: 是否启用思考。
            reasoning_effort_override: 本次调用临时替换的推理力度档位；
                None 时沿用 provider 的 reasoning_effort。
            call: 当前独立调用尝试上下文。

        Returns:
            归一化后的 LLM 响应。
        """
        ...
