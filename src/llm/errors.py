"""LLM 调用的统一错误域与安全诊断信息。"""

from __future__ import annotations

import asyncio
import re
import socket
import uuid
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Iterable, Mapping

import anthropic
import httpx
import openai

_CONTROL_FLOW_ERRORS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_MAX_SAFE_MESSAGE_LENGTH = 500
_MAX_SAFE_METADATA_LENGTH = 200


class LLMErrorKind(StrEnum):
    """LLM 调用错误的稳定分类。"""

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVICE = "service"
    RESPONSE_PROTOCOL = "response_protocol"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    BILLING_QUOTA = "billing_quota"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNPROCESSABLE = "unprocessable"
    CONTEXT_LIMIT = "context_limit"
    OUTPUT_LIMIT = "output_limit"
    CONTENT_POLICY = "content_policy"
    UNKNOWN = "unknown"


_RETRYABLE_KINDS = {
    LLMErrorKind.NETWORK,
    LLMErrorKind.TIMEOUT,
    LLMErrorKind.RATE_LIMIT,
    LLMErrorKind.SERVICE,
    LLMErrorKind.RESPONSE_PROTOCOL,
}


@dataclass(frozen=True, slots=True)
class LLMErrorInfo:
    """一次底层异常的安全结构化表示。"""

    kind: LLMErrorKind
    message: str
    retryable: bool
    status_code: int | None = None
    provider_code: str | None = None
    request_id: str | None = None
    retry_after: str | None = None
    retry_after_ms: float | None = None
    original_exception_type: str = "Exception"

    @property
    def status(self) -> int | None:
        """返回 HTTP 状态码。

        Returns:
            HTTP 状态码；未知时为 None。
        """
        return self.status_code


class LLMCallError(RuntimeError):
    """LLM 调用无法继续后的统一终态异常。"""

    def __init__(
        self,
        *,
        info: LLMErrorInfo,
        attempts: int,
        partial_output: str = "",
        diagnostic_id: str | None = None,
    ) -> None:
        """初始化终态调用异常。

        Args:
            info: 已安全化的结构化错误信息。
            attempts: 实际执行的尝试次数。
            partial_output: 最后一轮已接收但未采用的正文片段。
            diagnostic_id: 日志关联 ID；缺省时自动生成。

        Returns:
            None。
        """
        self.info = info
        self.attempts = attempts
        self.partial_output = partial_output
        self.diagnostic_id = diagnostic_id or f"llm_{uuid.uuid4().hex[:12]}"
        super().__init__(
            f"LLM 调用失败 [{info.kind.value}]：{info.message} "
            f"(diagnostic_id={self.diagnostic_id}, attempts={attempts})"
        )


class LLMConfigurationError(LLMCallError):
    """LLM 调用前即可确定的不可重试配置异常。"""

    def __init__(self, message: str, *, diagnostic_id: str | None = None) -> None:
        """初始化配置异常。

        Args:
            message: 面向调用方的安全配置错误说明。
            diagnostic_id: 日志关联 ID；缺省时自动生成。

        Returns:
            None。
        """
        info = LLMErrorInfo(
            kind=LLMErrorKind.BAD_REQUEST,
            message=_sanitize_message(message),
            retryable=False,
            original_exception_type=_safe_exception_type_name(self),
        )
        super().__init__(info=info, attempts=0, diagnostic_id=diagnostic_id)


class LLMStreamResponseError(RuntimeError):
    """流式响应不完整、不可解析或违反 provider 协议。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """初始化仅携带有限供应商元数据的流响应异常。

        Args:
            message: provider 返回的错误摘要或内部协议说明。
            code: 可选供应商错误码。
            status_code: 可选 HTTP 状态码。
            request_id: 可选供应商请求 ID。

        Returns:
            None。
        """
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.error = {"code": code, "message": message}


LLMResponseProtocolError = LLMStreamResponseError


_CONTEXT_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "input_too_long",
    "model_context_window_exceeded",
    "overlong_prompt",
    "prompt_too_long",
    "token_limit_exceeded",
}
_OUTPUT_CODES = {
    "length_finish_reason",
    "max_output_tokens",
    "max_output_tokens_exceeded",
    "output_limit_exceeded",
}
_CONTENT_POLICY_CODES = {
    "content_filter",
    "content_filter_error",
    "content_policy_violation",
    "content_policy_error",
    "image_content_policy_violation",
    "refusal",
    "safety_violation",
}
_BILLING_CODES = {
    "billing_error",
    "billing_hard_limit_reached",
    "credit_balance_too_low",
    "exceeded_current_quota_error",
    "insufficient_credit",
    "insufficient_quota",
    "quota_exceeded",
}
_RATE_LIMIT_CODES = {
    "rate_limit_error",
    "rate_limit_exceeded",
    "rate_limit_reached_error",
    "requests_limit_exceeded",
    "tokens_limit_exceeded",
}
_SERVICE_CODES = {
    "engine_overloaded_error",
    "insufficient_system_resource",
    "internal_error",
    "internal_server_error",
    "overloaded_error",
    "server_error",
    "service_unavailable",
}
_AUTHENTICATION_CODES = {
    "authentication_error",
    "invalid_api_key",
    "invalid_authentication",
    "unauthorized",
}
_PERMISSION_CODES = {"forbidden", "permission_denied", "permission_error"}
_NOT_FOUND_CODES = {
    "image_file_not_found",
    "model_not_found",
    "not_found",
    "not_found_error",
}
_PAYLOAD_CODES = {
    "image_file_too_large",
    "image_too_large",
    "payload_too_large",
    "request_too_large",
}
_UNPROCESSABLE_CODES = {"unprocessable_entity", "unprocessable_entity_error"}
_BAD_REQUEST_CODES = {
    "bad_request",
    "bad_request_error",
    "empty_image_file",
    "image_parse_error",
    "image_too_small",
    "invalid_base64_image",
    "invalid_image",
    "invalid_image_format",
    "invalid_image_mode",
    "invalid_image_url",
    "invalid_prompt",
    "invalid_request",
    "invalid_request_error",
    "unsupported_image_media_type",
}
_PROTOCOL_CODES = {
    "invalid_response",
    "invalid_response_error",
    "response_validation_error",
    "stream_read_error",
}

_CONTEXT_TEXT_PATTERNS = (
    "context length",
    "context window",
    "input is too long",
    "input token length",
    "maximum context",
    "model token limit",
    "overlong prompt",
    "prompt too long",
    "too many input tokens",
    "tokens exceed the model",
)
_OUTPUT_TEXT_PATTERNS = (
    "maximum output tokens",
    "max output tokens",
    "output token limit",
    "response length limit",
)
_CONTENT_POLICY_TEXT_PATTERNS = (
    "blocked by content policy",
    "content filter",
    "content policy",
    "safety policy violation",
)


def classify_llm_error(exc: BaseException) -> LLMErrorInfo:
    """按固定优先级把底层异常分类并提取安全诊断字段。

    Args:
        exc: SDK、网络层或 provider 流解析抛出的原始异常。

    Returns:
        不包含完整请求、响应体或凭据的结构化错误信息。

    Raises:
        asyncio.CancelledError: 输入为任务取消异常时原样传播。
        KeyboardInterrupt: 输入为键盘中断时原样传播。
        SystemExit: 输入为进程退出异常时原样传播。
    """
    if isinstance(exc, _CONTROL_FLOW_ERRORS):
        raise exc
    try:
        return _classify_llm_error(exc)
    except Exception:
        exception_type = _safe_exception_type_name(exc)
        return LLMErrorInfo(
            kind=LLMErrorKind.UNKNOWN,
            message=exception_type,
            retryable=False,
            original_exception_type=exception_type,
        )


def _classify_llm_error(exc: BaseException) -> LLMErrorInfo:
    """执行允许降级失败的内部分类流程。

    Args:
        exc: 非控制流原始异常。

    Returns:
        结构化且已安全化的错误信息。
    """
    if isinstance(exc, LLMCallError):
        exception_type = _safe_exception_type_name(exc)
        fallback_info = LLMErrorInfo(
            kind=LLMErrorKind.UNKNOWN,
            message=exception_type,
            retryable=False,
            original_exception_type=exception_type,
        )
        info = _safe_getattr(exc, "info", fallback_info)
        return info if isinstance(info, LLMErrorInfo) else fallback_info

    matched_exception = exc
    structured_message = _extract_structured_message(exc)
    raw_message = structured_message or _safe_string(exc, _safe_exception_type_name(exc))
    provider_codes = _extract_provider_codes(exc)

    kind = _classify_provider_codes(provider_codes, raw_message)
    if kind is None:
        kind = _classify_sdk_type(exc)
    if kind is None:
        kind = _classify_http_status(_extract_status_code(exc))
    if kind is None:
        chain_match = _classify_exception_chain(exc)
        if chain_match is not None:
            kind, matched_exception = chain_match
    if kind is None:
        text_match = _classify_text_chain(exc)
        if text_match is not None:
            kind, matched_exception = text_match
    if kind is None:
        kind = LLMErrorKind.UNKNOWN

    matched_message = _extract_structured_message(matched_exception)
    safe_message = (
        _safe_exception_type_name(exc)
        if kind is LLMErrorKind.UNKNOWN
        else matched_message or _safe_unstructured_message(matched_exception)
    )
    matched_codes = _extract_provider_codes(matched_exception)
    provider_code = _preferred_provider_code(
        matched_codes,
        matched_message
        or _safe_string(matched_exception, _safe_exception_type_name(matched_exception)),
    )
    status_code = _extract_status_code(matched_exception)
    headers = _extract_headers(matched_exception)
    request_id = _extract_request_id(matched_exception, headers)
    retry_after = _sanitize_external_metadata(_header_value(headers, "retry-after"))
    retry_after_ms = _parse_non_negative_float(
        _sanitize_external_metadata(_header_value(headers, "retry-after-ms"))
    )
    return LLMErrorInfo(
        kind=kind,
        message=_sanitize_message(safe_message),
        retryable=kind in _RETRYABLE_KINDS,
        status_code=status_code,
        provider_code=_sanitize_external_metadata(provider_code),
        request_id=_sanitize_external_metadata(request_id),
        retry_after=retry_after,
        retry_after_ms=retry_after_ms,
        original_exception_type=_safe_exception_type_name(exc),
    )


def _classify_provider_codes(codes: Iterable[str], message: str) -> LLMErrorKind | None:
    """根据供应商 code/type 与同一结构化错误消息分类。

    Args:
        codes: 供应商 code/type 候选序列。
        message: 供应商结构化错误消息。

    Returns:
        已识别错误类别；无法识别时为 None。
    """
    normalized_codes = [_normalize_code(code) for code in codes]
    semantic_text_kind = _classify_semantic_limit_text(message)
    for code in normalized_codes:
        if code in _CONTEXT_CODES:
            return LLMErrorKind.CONTEXT_LIMIT
        if code in _OUTPUT_CODES:
            return LLMErrorKind.OUTPUT_LIMIT
        if code in _CONTENT_POLICY_CODES:
            return LLMErrorKind.CONTENT_POLICY
        if code in _BILLING_CODES:
            return LLMErrorKind.BILLING_QUOTA
        if code in _RATE_LIMIT_CODES:
            return LLMErrorKind.RATE_LIMIT
        if code in _SERVICE_CODES:
            return LLMErrorKind.SERVICE
        if code in _AUTHENTICATION_CODES:
            return LLMErrorKind.AUTHENTICATION
        if code in _PERMISSION_CODES:
            return LLMErrorKind.PERMISSION
        if code in _NOT_FOUND_CODES:
            return LLMErrorKind.NOT_FOUND
        if code in _PAYLOAD_CODES:
            return LLMErrorKind.PAYLOAD_TOO_LARGE
        if code in _UNPROCESSABLE_CODES:
            return LLMErrorKind.UNPROCESSABLE
        if code in _PROTOCOL_CODES:
            return LLMErrorKind.RESPONSE_PROTOCOL
        if code in _BAD_REQUEST_CODES:
            return semantic_text_kind or LLMErrorKind.BAD_REQUEST
    return None


def _classify_sdk_type(exc: BaseException) -> LLMErrorKind | None:
    """根据顶层 SDK 或网络异常类型分类。

    Args:
        exc: 顶层异常。

    Returns:
        已识别错误类别；无法识别时为 None。
    """
    if isinstance(exc, LLMStreamResponseError):
        return LLMErrorKind.RESPONSE_PROTOCOL
    if isinstance(exc, (openai.APIResponseValidationError, anthropic.APIResponseValidationError)):
        return LLMErrorKind.RESPONSE_PROTOCOL
    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return LLMErrorKind.CONTENT_POLICY
    if isinstance(exc, openai.LengthFinishReasonError):
        return LLMErrorKind.OUTPUT_LIMIT
    if isinstance(exc, (openai.AuthenticationError, anthropic.AuthenticationError)):
        return LLMErrorKind.AUTHENTICATION
    if isinstance(exc, (openai.PermissionDeniedError, anthropic.PermissionDeniedError)):
        return LLMErrorKind.PERMISSION
    if isinstance(exc, (openai.NotFoundError, anthropic.NotFoundError)):
        return LLMErrorKind.NOT_FOUND
    if isinstance(exc, (openai.UnprocessableEntityError, anthropic.UnprocessableEntityError)):
        return LLMErrorKind.UNPROCESSABLE
    if isinstance(exc, (openai.BadRequestError, anthropic.BadRequestError)):
        message = _extract_structured_message(exc) or _safe_string(exc, "")
        return _classify_semantic_limit_text(message) or LLMErrorKind.BAD_REQUEST
    if isinstance(exc, (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return LLMErrorKind.TIMEOUT
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return LLMErrorKind.NETWORK
    if isinstance(exc, (openai.RateLimitError, anthropic.RateLimitError)):
        return LLMErrorKind.RATE_LIMIT
    if isinstance(exc, (openai.InternalServerError, anthropic.InternalServerError, openai.ConflictError, anthropic.ConflictError)):
        return LLMErrorKind.SERVICE
    if isinstance(exc, httpx.TransportError):
        return LLMErrorKind.NETWORK
    if isinstance(exc, ConnectionError):
        return LLMErrorKind.NETWORK
    if isinstance(exc, socket.gaierror):
        return LLMErrorKind.NETWORK
    return None


def _classify_http_status(status_code: int | None) -> LLMErrorKind | None:
    """根据 HTTP 状态码分类。

    Args:
        status_code: HTTP 状态码。

    Returns:
        已识别错误类别；无法识别时为 None。
    """
    if status_code == 400:
        return LLMErrorKind.BAD_REQUEST
    if status_code == 401:
        return LLMErrorKind.AUTHENTICATION
    if status_code == 402:
        return LLMErrorKind.BILLING_QUOTA
    if status_code == 403:
        return LLMErrorKind.PERMISSION
    if status_code == 404:
        return LLMErrorKind.NOT_FOUND
    if status_code == 408:
        return LLMErrorKind.TIMEOUT
    if status_code == 409:
        return LLMErrorKind.SERVICE
    if status_code == 413:
        return LLMErrorKind.PAYLOAD_TOO_LARGE
    if status_code == 422:
        return LLMErrorKind.UNPROCESSABLE
    if status_code == 429:
        return LLMErrorKind.RATE_LIMIT
    if status_code == 529 or status_code is not None and 500 <= status_code <= 599:
        return LLMErrorKind.SERVICE
    return None


def _classify_exception_chain(
    exc: BaseException,
) -> tuple[LLMErrorKind, BaseException] | None:
    """在顶层信号不足时检查 cause/context 链。

    Args:
        exc: 顶层异常。

    Returns:
        链中首个已识别错误类别及实际命中异常；无法识别时为 None。
    """
    for nested in list(_iter_exception_chain(exc))[1:]:
        message = _extract_structured_message(nested) or _safe_string(nested, "")
        codes = _extract_provider_codes(nested)
        kind = _classify_provider_codes(codes, message)
        if kind is None:
            kind = _classify_sdk_type(nested)
        if kind is None:
            kind = _classify_http_status(_extract_status_code(nested))
        if kind is not None:
            return kind, nested
    return None


def _classify_text_chain(
    exc: BaseException,
) -> tuple[LLMErrorKind, BaseException] | None:
    """逐层执行保守文本分类并保留实际命中异常。

    Args:
        exc: 顶层异常。

    Returns:
        首个文本命中的错误类别及异常；无法识别时为 None。
    """
    for nested in _iter_exception_chain(exc):
        kind = _classify_text(_safe_string(nested, ""))
        if kind is not None:
            return kind, nested
    return None


def _classify_text(text: str) -> LLMErrorKind | None:
    """用保守文本规则处理没有结构化信号的异常。

    Args:
        text: 顶层和异常链的组合文本。

    Returns:
        已识别错误类别；无法可靠识别时为 None。
    """
    lowered = text.lower()
    semantic_kind = _classify_semantic_limit_text(lowered)
    if semantic_kind is not None:
        return semantic_kind
    if any(pattern in lowered for pattern in ("timed out", "timeout", "time out")):
        return LLMErrorKind.TIMEOUT
    if any(pattern in lowered for pattern in ("connection reset", "connection refused", "network is unreachable", "dns resolution")):
        return LLMErrorKind.NETWORK
    return None


def _classify_semantic_limit_text(text: str) -> LLMErrorKind | None:
    """识别上下文、输出长度与内容策略的明确文本信号。

    Args:
        text: 供应商消息或异常文本。

    Returns:
        限制类错误类别；没有明确命中时为 None。
    """
    lowered = text.lower().replace("_", " ")
    if any(pattern in lowered for pattern in _CONTEXT_TEXT_PATTERNS):
        return LLMErrorKind.CONTEXT_LIMIT
    if any(pattern in lowered for pattern in _OUTPUT_TEXT_PATTERNS):
        return LLMErrorKind.OUTPUT_LIMIT
    if any(pattern in lowered for pattern in _CONTENT_POLICY_TEXT_PATTERNS):
        return LLMErrorKind.CONTENT_POLICY
    return None


def _extract_provider_codes(exc: BaseException) -> list[str]:
    """从异常属性和结构化响应体提取 code/type 候选。

    Args:
        exc: 待检查异常。

    Returns:
        按供应商字段优先级排列且去重的 code/type 列表。
    """
    values: list[str] = []
    for source in (_safe_getattr(exc, "body"), _safe_getattr(exc, "error"), exc):
        values.extend(_code_values(source))
    return list(dict.fromkeys(value for value in values if value))


def _code_values(value: Any) -> list[str]:
    """从单个对象读取有限层级的 code/type 字段。

    Args:
        value: 字典、SDK 错误对象或其他值。

    Returns:
        找到的字符串 code/type 值。
    """
    if isinstance(value, Mapping):
        nested_error = _safe_mapping_get(value, "error")
        sources = [nested_error, value] if nested_error is not None else [value]
        result: list[str] = []
        for source in sources:
            if isinstance(source, Mapping):
                for key in ("code", "type"):
                    candidate = _safe_mapping_get(source, key)
                    candidate_text = _safe_string(candidate, "") if isinstance(candidate, str) else ""
                    if candidate_text.strip():
                        result.append(candidate_text.strip())
        return result
    result = []
    for name in ("code", "type"):
        candidate = _safe_getattr(value, name)
        candidate_text = _safe_string(candidate, "") if isinstance(candidate, str) else ""
        if candidate_text.strip():
            result.append(candidate_text.strip())
    return result


def _preferred_provider_code(codes: Iterable[str], message: str) -> str | None:
    """选择最能表达最终分类的供应商语义码。

    Args:
        codes: code/type 候选序列。
        message: 同一供应商错误消息。

    Returns:
        首个可识别码；均无法识别时返回首个码或 None。
    """
    code_list = list(codes)
    for code in code_list:
        if _classify_provider_codes([code], message) is not None:
            return code
    return code_list[0] if code_list else None


def _extract_structured_message(exc: BaseException) -> str | None:
    """只从允许的结构化 message 字段提取供应商摘要。

    Args:
        exc: 待检查异常。

    Returns:
        供应商 message 字符串；不存在时为 None。
    """
    for source in (_safe_getattr(exc, "body"), _safe_getattr(exc, "error")):
        message = _message_value(source)
        if message:
            return message
    return None


def _safe_unstructured_message(exc: BaseException) -> str:
    """在没有标准 message 字段时生成不包含请求或响应体的摘要。

    Args:
        exc: 待安全化异常。

    Returns:
        异常自身文本，或检测到响应体时仅返回异常类型。
    """
    response = _safe_getattr(exc, "response")
    has_response_text = _safe_getattr(response, "text") is not None
    if _safe_getattr(exc, "body") is not None or has_response_text:
        return _safe_exception_type_name(exc)
    return _safe_string(exc, _safe_exception_type_name(exc))


def _message_value(value: Any) -> str | None:
    """读取字典或对象中受控的 message 字段。

    Args:
        value: 供应商错误结构。

    Returns:
        message 字符串；不存在时为 None。
    """
    if isinstance(value, Mapping):
        nested = _safe_mapping_get(value, "error")
        if nested is not None:
            nested_message = _message_value(nested)
            if nested_message:
                return nested_message
        message = _safe_mapping_get(value, "message")
        message_text = _safe_string(message, "") if isinstance(message, str) else ""
        return message_text if message_text.strip() else None
    message = _safe_getattr(value, "message")
    message_text = _safe_string(message, "") if isinstance(message, str) else ""
    return message_text if message_text.strip() else None


def _extract_status_code(exc: BaseException) -> int | None:
    """从异常或响应对象提取 HTTP 状态码。

    Args:
        exc: 待检查异常。

    Returns:
        HTTP 状态码；未知时为 None。
    """
    status_code = _safe_getattr(exc, "status_code")
    if isinstance(status_code, int):
        return status_code
    response = _safe_getattr(exc, "response")
    response_status = _safe_getattr(response, "status_code")
    return response_status if isinstance(response_status, int) else None


def _extract_headers(exc: BaseException) -> Mapping[str, Any]:
    """从异常或响应对象提取响应头映射。

    Args:
        exc: 待检查异常。

    Returns:
        响应头映射；不存在时为空字典。
    """
    response = _safe_getattr(exc, "response")
    headers = _safe_getattr(response, "headers")
    if headers is None:
        headers = _safe_getattr(exc, "headers")
    return headers if isinstance(headers, Mapping) else {}


def _header_value(headers: Mapping[str, Any], name: str) -> str | None:
    """以不区分大小写方式读取单个响应头。

    Args:
        headers: 响应头映射。
        name: 目标响应头名称。

    Returns:
        去除空白后的响应头值；不存在时为 None。
    """
    lowered_name = name.lower()
    for key, value in _safe_mapping_items(headers):
        if _safe_string(key, "").lower() == lowered_name and value is not None:
            text = _safe_string(value, "").strip()
            return text or None
    return None


def _extract_request_id(exc: BaseException, headers: Mapping[str, Any]) -> str | None:
    """从 SDK 属性或常见响应头提取请求 ID。

    Args:
        exc: 待检查异常。
        headers: 已提取的响应头。

    Returns:
        请求 ID；不存在时为 None。
    """
    for name in ("request_id", "_request_id"):
        value = _safe_getattr(exc, name)
        value_text = _safe_string(value, "") if isinstance(value, str) else ""
        if value_text.strip():
            return value_text.strip()
    for name in ("x-request-id", "request-id", "cf-ray"):
        value = _header_value(headers, name)
        if value:
            return value
    return None


def _parse_non_negative_float(value: str | None) -> float | None:
    """把响应头转换为非负浮点数。

    Args:
        value: 原始响应头值。

    Returns:
        非负浮点数；格式非法时为 None。
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def safe_exception_traceback(exc: BaseException) -> TracebackType | None:
    """安全读取异常堆栈供诊断日志使用。

    Args:
        exc: 需要读取堆栈的原始异常。

    Returns:
        合法 traceback；getter 缺失、返回非法类型或抛错时为 None。
    """
    try:
        traceback = exc.__traceback__
    except Exception:
        return None
    return traceback if isinstance(traceback, TracebackType) else None


def _iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    """以 cause 优先顺序遍历异常链并防止环。

    Args:
        exc: 顶层异常。

    Returns:
        最多八层且不重复的异常迭代器。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        previous = current
        current = _safe_getattr(previous, "__cause__")
        suppress_context = bool(_safe_getattr(previous, "__suppress_context__", False))
        if current is None and not suppress_context:
            current = _safe_getattr(previous, "__context__")
        if current is not None and not isinstance(current, BaseException):
            return


def _normalize_code(code: str) -> str:
    """把供应商错误码归一化为小写下划线形式。

    Args:
        code: 原始供应商错误码。

    Returns:
        归一化错误码。
    """
    safe_code = _safe_string(code, "")
    return re.sub(r"[^a-z0-9]+", "_", safe_code.strip().lower()).strip("_")


def _sanitize_message(message: str) -> str:
    """移除凭据、URL userinfo 并限制错误摘要长度。

    Args:
        message: 原始或结构化异常消息。

    Returns:
        可安全展示和记录的单段错误摘要。
    """
    safe = _safe_string(message, "LLM 调用失败").replace("\r", " ").replace("\n", " ")
    substitutions = (
        (
            r"(?i)\b(https?://)[^/\s:@]+:[^@/\s]+@",
            r"\1[REDACTED]@",
        ),
        (
            r"(?i)(\b(?:proxy-)?authorization\b[\"']?\s*[:=]\s*)[^;}\]]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]"),
        (r"(?i)\btoken\s+[A-Za-z0-9._~+/=-]+", "Token [REDACTED]"),
        (r"(?i)\bsk-[A-Za-z0-9_-]{4,}", "[REDACTED]"),
        (
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
            r"password|secret)\b[\"']?\s*[:=]\s*)"
            r"(?:\[REDACTED\]|\"[^\"]*\"|'[^']*'|[^,;\s&{}\]#]+)",
            r"\1[REDACTED]",
        ),
    )
    for pattern, replacement in substitutions:
        safe = re.sub(pattern, replacement, safe)
    safe = " ".join(safe.split())
    if len(safe) > _MAX_SAFE_MESSAGE_LENGTH:
        return safe[: _MAX_SAFE_MESSAGE_LENGTH - 1] + "…"
    return safe or "LLM 调用失败"


def _sanitize_external_metadata(value: object | None) -> str | None:
    """安全化并限制供应商返回的短元数据字段。

    Args:
        value: provider code、请求 ID 或重试响应头值。

    Returns:
        去除换行和凭据后的限长字符串；缺失或无法转换时为 None。
    """
    if value is None:
        return None
    raw = _safe_string(value, "")
    if not raw:
        return None
    safe = _sanitize_message(raw)
    if len(safe) > _MAX_SAFE_METADATA_LENGTH:
        return safe[: _MAX_SAFE_METADATA_LENGTH - 1] + "…"
    return safe


def _safe_exception_type_name(exc: BaseException) -> str:
    """读取异常类型名并在元类 getter 失败时使用固定回退。

    Args:
        exc: 待读取类型名的异常。

    Returns:
        已安全化的异常类型名；无法读取时固定为 Exception。
    """
    try:
        raw_name = type(exc).__name__
    except Exception:
        return "Exception"
    return _sanitize_external_metadata(raw_name) or "Exception"


def _safe_string(value: object, fallback: str) -> str:
    """把不可信对象转换为字符串且吞掉转换异常。

    Args:
        value: 待转换对象。
        fallback: 转换失败时返回的安全文本。

    Returns:
        成功转换的字符串或 fallback。
    """
    try:
        return str(value)
    except Exception:
        return fallback


def _safe_getattr(value: object, name: str, default: Any = None) -> Any:
    """读取不可信对象属性且吞掉 getter 异常。

    Args:
        value: 待读取对象。
        name: 属性名。
        default: 属性缺失或 getter 失败时的默认值。

    Returns:
        属性值或 default。
    """
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _safe_mapping_get(value: Mapping[Any, Any], key: object, default: Any = None) -> Any:
    """读取不可信映射键且吞掉映射实现异常。

    Args:
        value: 待读取映射。
        key: 目标键。
        default: 读取失败时的默认值。

    Returns:
        键值或 default。
    """
    try:
        return value.get(key, default)
    except Exception:
        return default


def _safe_mapping_items(value: Mapping[Any, Any]) -> tuple[tuple[Any, Any], ...]:
    """复制不可信映射条目且吞掉遍历异常。

    Args:
        value: 待遍历映射。

    Returns:
        可安全遍历的键值元组；失败时为空元组。
    """
    try:
        return tuple(value.items())
    except Exception:
        return ()
