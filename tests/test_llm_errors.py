"""LLM 统一错误分类与安全诊断信息测试。"""

from __future__ import annotations

import asyncio
import errno
import socket
from types import SimpleNamespace

import httpx
import openai
import pytest

from src.llm.errors import (
    LLMCallError,
    LLMErrorKind,
    LLMStreamResponseError,
    classify_llm_error,
    safe_exception_traceback,
)


class ProviderError(Exception):
    """模拟同时携带供应商响应字段的异常。"""

    def __init__(
        self,
        message: str,
        *,
        body: object | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> None:
        """初始化测试异常。

        Args:
            message: 异常文本。
            body: 供应商结构化响应体。
            status_code: HTTP 状态码。
            headers: 响应头。
            request_id: SDK 暴露的请求 ID。

        Returns:
            None。
        """
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.request_id = request_id
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


class ExplodingStringError(Exception):
    """字符串转换会抛出异常的畸形异常。"""

    def __str__(self) -> str:
        """模拟不可信异常的字符串转换失败。

        Returns:
            本方法不会返回。

        Raises:
            RuntimeError: 每次字符串转换固定抛出。
        """
        raise RuntimeError("malicious __str__")


class ExplodingAttributesError(Exception):
    """常见 SDK 元数据属性读取都会失败的畸形异常。"""

    def __str__(self) -> str:
        """返回无害的外层错误文本。

        Returns:
            固定错误文本。
        """
        return "malformed provider error"

    def __getattribute__(self, name: str):
        """让外部元数据属性 getter 固定抛出。

        Args:
            name: 待读取属性名。

        Returns:
            非危险属性交给异常基类读取。

        Raises:
            RuntimeError: 读取外部元数据属性时固定抛出。
        """
        if name in {
            "body",
            "response",
            "headers",
            "request_id",
            "_request_id",
            "error",
            "code",
            "type",
            "status_code",
        }:
            raise RuntimeError(f"dangerous getter: {name}")
        return super().__getattribute__(name)


class ExplodingMapping(dict):
    """读取键或遍历条目都会失败的畸形映射。"""

    def get(self, key: object, default: object = None) -> object:
        """模拟映射键读取失败。

        Args:
            key: 待读取键。
            default: 缺失键默认值。

        Returns:
            本方法不会返回。

        Raises:
            RuntimeError: 每次读取固定抛出。
        """
        raise RuntimeError("dangerous mapping get")

    def items(self):
        """模拟映射条目遍历失败。

        Returns:
            本方法不会返回。

        Raises:
            RuntimeError: 每次遍历固定抛出。
        """
        raise RuntimeError("dangerous mapping items")


class ControlFlowStringError(Exception):
    """字符串转换会抛出指定控制流异常的测试异常。"""

    def __init__(self, control_error: BaseException) -> None:
        """保存待抛出的控制流异常。

        Args:
            control_error: 字符串转换时原样抛出的异常。

        Returns:
            None。
        """
        super().__init__()
        self.control_error = control_error

    def __str__(self) -> str:
        """原样抛出预设控制流异常。

        Returns:
            本方法不会返回。

        Raises:
            BaseException: 初始化时传入的控制流异常。
        """
        raise self.control_error


class ControlFlowGetterError(Exception):
    """元数据 getter 会抛出指定控制流异常的测试异常。"""

    def __init__(self, control_error: BaseException) -> None:
        """保存待抛出的控制流异常。

        Args:
            control_error: 读取 body 时原样抛出的异常。

        Returns:
            None。
        """
        super().__init__("getter control flow")
        self.control_error = control_error

    @property
    def body(self) -> object:
        """原样抛出预设控制流异常。

        Returns:
            本属性不会返回。

        Raises:
            BaseException: 初始化时传入的控制流异常。
        """
        raise self.control_error


class ExplodingTypeNameMeta(type):
    """读取异常类名时抛出普通异常的测试元类。"""

    def __getattribute__(cls, name: str) -> object:
        """仅阻止读取类名，其余属性正常返回。

        Args:
            name: 待读取元类属性名。

        Returns:
            非类名属性的正常值。

        Raises:
            RuntimeError: 读取 __name__ 时固定抛出。
        """
        if name == "__name__":
            raise RuntimeError("dangerous type name")
        return super().__getattribute__(name)


class ExplodingTypeNameError(Exception, metaclass=ExplodingTypeNameMeta):
    """异常类型名自身不可信的测试异常。"""

    def __str__(self) -> str:
        """同时模拟异常字符串转换失败。

        Returns:
            本方法不会返回。

        Raises:
            RuntimeError: 每次字符串转换固定抛出。
        """
        raise RuntimeError("dangerous exception string")


def test_provider_semantic_code_precedes_sdk_type_and_http_status() -> None:
    """供应商语义码应优先于 SDK 类型与 HTTP 状态。

    Returns:
        None。
    """
    request = httpx.Request("POST", "https://example.test/v1/chat")
    response = httpx.Response(429, request=request)
    error = openai.RateLimitError(
        "rate limited",
        response=response,
        body={"error": {"type": "context_length_exceeded", "message": "too long"}},
    )

    info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.CONTEXT_LIMIT
    assert info.provider_code == "context_length_exceeded"
    assert info.retryable is False


@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (400, LLMErrorKind.BAD_REQUEST, False),
        (401, LLMErrorKind.AUTHENTICATION, False),
        (402, LLMErrorKind.BILLING_QUOTA, False),
        (403, LLMErrorKind.PERMISSION, False),
        (404, LLMErrorKind.NOT_FOUND, False),
        (408, LLMErrorKind.TIMEOUT, True),
        (409, LLMErrorKind.SERVICE, True),
        (413, LLMErrorKind.PAYLOAD_TOO_LARGE, False),
        (422, LLMErrorKind.UNPROCESSABLE, False),
        (429, LLMErrorKind.RATE_LIMIT, True),
        (500, LLMErrorKind.SERVICE, True),
        (529, LLMErrorKind.SERVICE, True),
    ],
)
def test_http_status_mapping(
    status_code: int,
    kind: LLMErrorKind,
    retryable: bool,
) -> None:
    """HTTP 状态应映射为固定错误类别与重试语义。

    Args:
        status_code: 待分类状态码。
        kind: 预期错误类别。
        retryable: 预期自动重试标志。

    Returns:
        None。
    """
    info = classify_llm_error(ProviderError("request failed", status_code=status_code))

    assert info.kind is kind
    assert info.retryable is retryable
    assert info.status_code == status_code


@pytest.mark.parametrize(
    ("provider_code", "kind", "retryable"),
    [
        ("insufficient_quota", LLMErrorKind.BILLING_QUOTA, False),
        ("billing_hard_limit_reached", LLMErrorKind.BILLING_QUOTA, False),
        ("billing_error", LLMErrorKind.BILLING_QUOTA, False),
        ("exceeded_current_quota_error", LLMErrorKind.BILLING_QUOTA, False),
        ("rate_limit_exceeded", LLMErrorKind.RATE_LIMIT, True),
        ("rate_limit_reached_error", LLMErrorKind.RATE_LIMIT, True),
        ("overloaded_error", LLMErrorKind.SERVICE, True),
        ("engine_overloaded_error", LLMErrorKind.SERVICE, True),
        ("max_output_tokens", LLMErrorKind.OUTPUT_LIMIT, False),
        ("content_policy_violation", LLMErrorKind.CONTENT_POLICY, False),
        ("content_filter", LLMErrorKind.CONTENT_POLICY, False),
    ],
)
def test_provider_semantic_codes(
    provider_code: str,
    kind: LLMErrorKind,
    retryable: bool,
) -> None:
    """供应商语义码应覆盖额度、限流、输出与策略错误。

    Args:
        provider_code: 供应商错误码。
        kind: 预期错误类别。
        retryable: 预期自动重试标志。

    Returns:
        None。
    """
    error = ProviderError(
        "provider rejected request",
        body={"error": {"code": provider_code, "message": "safe summary"}},
        status_code=400,
    )

    info = classify_llm_error(error)

    assert info.kind is kind
    assert info.provider_code == provider_code
    assert info.retryable is retryable


def test_sdk_type_precedes_conflicting_http_status() -> None:
    """SDK 认证异常应优先于冲突的 HTTP 状态。

    Returns:
        None。
    """
    request = httpx.Request("POST", "https://example.test/v1/chat")
    response = httpx.Response(500, request=request)
    error = openai.AuthenticationError("bad credentials", response=response, body=None)

    info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.AUTHENTICATION
    assert info.retryable is False


def test_cause_chain_is_classified_before_text_fallback() -> None:
    """包装异常应从 cause 链识别超时，不被外层文本误导。

    Returns:
        None。
    """
    try:
        try:
            raise TimeoutError("socket timed out")
        except TimeoutError as cause:
            raise RuntimeError("unknown wrapper") from cause
    except RuntimeError as error:
        info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.TIMEOUT
    assert info.retryable is True
    assert info.original_exception_type == "RuntimeError"


def test_cause_chain_uses_matched_exception_metadata_and_safe_message() -> None:
    """嵌套命中异常应提供全部元数据且保留最外层异常类型。

    Returns:
        None。
    """
    nested = ProviderError(
        "unsafe outer rendering sk-do-not-leak",
        body={
            "error": {
                "code": "rate_limit_reached_error",
                "message": "rate limited for key sk-provider-secret",
            }
        },
        status_code=429,
        headers={"retry-after": "8", "retry-after-ms": "1500"},
        request_id="req_nested",
    )
    try:
        raise RuntimeError("provider wrapper") from nested
    except RuntimeError as error:
        info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.RATE_LIMIT
    assert info.status_code == 429
    assert info.provider_code == "rate_limit_reached_error"
    assert info.request_id == "req_nested"
    assert info.retry_after == "8"
    assert info.retry_after_ms == 1500
    assert info.message == "rate limited for key [REDACTED]"
    assert info.original_exception_type == "RuntimeError"


def test_multilevel_context_chain_is_fully_traversed() -> None:
    """多层隐式 context 链末端的网络异常仍应被识别。

    Returns:
        None。
    """
    network = ConnectionError("connection reset by peer")
    middle = RuntimeError("middle")
    middle.__context__ = network
    outer = RuntimeError("outer")
    outer.__context__ = middle

    info = classify_llm_error(outer)

    assert info.kind is LLMErrorKind.NETWORK
    assert info.message == "connection reset by peer"
    assert info.original_exception_type == "RuntimeError"


def test_response_protocol_error_is_retryable() -> None:
    """内部流响应协议异常应允许自动重试。

    Returns:
        None。
    """
    info = classify_llm_error(LLMStreamResponseError("invalid stream frame"))

    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


def test_sdk_response_validation_error_is_retryable_protocol_failure() -> None:
    """SDK 响应校验异常应归入可重试响应协议错误。

    Returns:
        None。
    """
    request = httpx.Request("POST", "https://example.test/v1/chat")
    response = httpx.Response(200, request=request)
    error = openai.APIResponseValidationError(response=response, body={"invalid": True})

    info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.RESPONSE_PROTOCOL
    assert info.retryable is True


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("input token length exceeds the model token limit", LLMErrorKind.CONTEXT_LIMIT),
        ("maximum context length exceeded", LLMErrorKind.CONTEXT_LIMIT),
        ("maximum output tokens reached", LLMErrorKind.OUTPUT_LIMIT),
        ("response blocked by content policy", LLMErrorKind.CONTENT_POLICY),
    ],
)
def test_conservative_text_fallback_for_semantic_limits(
    message: str,
    kind: LLMErrorKind,
) -> None:
    """缺少结构化信号时只识别明确的限制类文本。

    Args:
        message: 供应商错误文本。
        kind: 预期错误类别。

    Returns:
        None。
    """
    info = classify_llm_error(RuntimeError(message))

    assert info.kind is kind
    assert info.retryable is False


def test_invalid_request_with_model_token_limit_is_context_error() -> None:
    """Kimi invalid_request_error 搭配 token limit 文本应归上下文超限。

    Returns:
        None。
    """
    error = ProviderError(
        "invalid request",
        body={
            "error": {
                "type": "invalid_request_error",
                "message": "input token length exceeds model token limit",
            }
        },
        status_code=400,
    )

    info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.CONTEXT_LIMIT
    assert info.retryable is False


def test_safe_metadata_extraction_redacts_secrets_and_full_body() -> None:
    """诊断信息应提取请求元数据且不泄漏密钥和完整响应体。

    Returns:
        None。
    """
    error = ProviderError(
        "Authorization: Bearer top-secret-token; api_key=sk-test-secret",
        body={
            "error": {
                "type": "rate_limit_exceeded",
                "message": "API key sk-provider-secret reached the request limit",
            },
            "echoed_prompt": "never include this complete request payload",
        },
        status_code=429,
        headers={
            "retry-after": "7",
            "retry-after-ms": "1250",
            "x-request-id": "req_header",
        },
        request_id="req_attribute",
    )

    info = classify_llm_error(error)

    assert info.request_id == "req_attribute"
    assert info.retry_after == "7"
    assert info.retry_after_ms == 1250
    assert "sk-provider-secret" not in info.message
    assert "sk-test-secret" not in info.message
    assert "top-secret-token" not in info.message
    assert "never include this" not in info.message


@pytest.mark.parametrize(
    ("unsafe_message", "secret"),
    [
        ("token=tok_live_SECRET", "tok_live_SECRET"),
        ("access_token=access_live_SECRET", "access_live_SECRET"),
        ("refresh_token=refresh_live_SECRET", "refresh_live_SECRET"),
        ("password=password_SECRET", "password_SECRET"),
        ("secret=generic_SECRET", "generic_SECRET"),
        (
            "https://user:password_SECRET@host.test/path",
            "user:password_SECRET",
        ),
        (
            "https://host.test/path?token=query_SECRET&other=1",
            "query_SECRET",
        ),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Proxy-Authorization: Custom proxy_SECRET", "proxy_SECRET"),
        ("Token token_scheme_SECRET", "token_scheme_SECRET"),
    ],
)
def test_safe_message_redacts_generic_credentials_and_url_userinfo(
    unsafe_message: str,
    secret: str,
) -> None:
    """结构化安全消息应脱敏通用凭据和 URL userinfo。

    Args:
        unsafe_message: 含凭据的供应商结构化消息。
        secret: 输出中不得出现的秘密值。

    Returns:
        None。
    """
    error = ProviderError(
        "provider rejected request",
        body={
            "error": {
                "type": "rate_limit_exceeded",
                "message": unsafe_message + " " + "x" * 600,
            }
        },
        status_code=429,
    )

    info = classify_llm_error(error)

    assert secret not in info.message
    assert "[REDACTED]" in info.message
    assert len(info.message) <= 500


def test_unstructured_body_is_never_used_as_safe_message() -> None:
    """无标准 message 字段的短响应体也不得进入安全错误摘要。

    Returns:
        None。
    """
    error = ProviderError(
        "raw response body: complete-private-response",
        body={"payload": "complete-private-response"},
        status_code=500,
    )

    info = classify_llm_error(error)

    assert "complete-private-response" not in info.message
    assert info.message == "ProviderError"


def test_unknown_error_is_non_retryable_and_call_error_keeps_only_safe_cause() -> None:
    """未知异常应保守失败，终态错误仅通过 cause 保留原异常。

    Returns:
        None。
    """
    original = RuntimeError("unexpected sk-secret-value")
    info = classify_llm_error(original)
    terminal = LLMCallError(
        info=info,
        attempts=2,
        partial_output="partial",
        diagnostic_id="diag_test",
    )

    assert info.kind is LLMErrorKind.UNKNOWN
    assert info.retryable is False
    assert terminal.info is info
    assert terminal.attempts == 2
    assert terminal.partial_output == "partial"
    assert terminal.diagnostic_id == "diag_test"
    assert not hasattr(terminal, "original_exception")
    assert "sk-secret-value" not in str(terminal)


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
def test_control_flow_errors_are_not_classified(control_error: BaseException) -> None:
    """取消与进程控制异常必须由分类入口原样传播。

    Args:
        control_error: 待验证的控制流异常。

    Returns:
        None。
    """
    with pytest.raises(type(control_error)) as raised:
        classify_llm_error(control_error)

    assert raised.value is control_error


@pytest.mark.parametrize(
    "malformed",
    [
        ExplodingStringError(),
        ExplodingAttributesError(),
        ProviderError("mapping wrapper", body=ExplodingMapping(), headers=ExplodingMapping()),
        ProviderError("headers wrapper", headers=ExplodingMapping({"x-test": "value"})),
    ],
)
def test_malformed_exceptions_never_escape_classifier(malformed: BaseException) -> None:
    """畸形字符串、getter 与映射不得从分类器裸抛。

    Args:
        malformed: 带恶意行为的异常实例。

    Returns:
        None。
    """
    info = classify_llm_error(malformed)

    assert info.kind is LLMErrorKind.UNKNOWN
    assert info.retryable is False
    assert info.original_exception_type == type(malformed).__name__


@pytest.mark.parametrize(
    "filesystem_error",
    [
        OSError(errno.ENOSPC, "No space left on device"),
        OSError("generic filesystem failure"),
    ],
)
def test_filesystem_os_errors_are_not_retryable(filesystem_error: OSError) -> None:
    """磁盘和普通文件系统 OSError 不得误判为网络错误。

    Args:
        filesystem_error: 文件系统类 OSError。

    Returns:
        None。
    """
    info = classify_llm_error(filesystem_error)

    assert info.kind is LLMErrorKind.UNKNOWN
    assert info.retryable is False


def test_dns_resolution_error_is_retryable_network_error() -> None:
    """明确的 DNS 解析异常应归为可重试网络错误。

    Returns:
        None。
    """
    info = classify_llm_error(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"))

    assert info.kind is LLMErrorKind.NETWORK
    assert info.retryable is True


def test_external_metadata_is_sanitized_and_length_limited() -> None:
    """供应商码、请求 ID 与重试头不得携带换行、凭据或超长文本。

    Returns:
        None。
    """
    injected_code = "rate_limit_exceeded\nBearer code-secret " + "x" * 500
    error = ProviderError(
        "metadata injection",
        body={
            "error": {
                "code": injected_code,
                "message": "safe line\nAuthorization: Bearer message-secret",
            }
        },
        status_code=429,
        headers={
            "retry-after": "7\nBearer retry-secret",
            "retry-after-ms": "not-a-number\napi_key=retry-ms-secret",
        },
        request_id="req-1\nBearer request-secret " + "y" * 500,
    )

    info = classify_llm_error(error)

    assert info.kind is LLMErrorKind.RATE_LIMIT
    for value in (info.message, info.provider_code, info.request_id, info.retry_after):
        assert value is not None
        assert "\n" not in value
        assert len(value) <= 200
    rendered = " ".join(
        value or "" for value in (info.message, info.provider_code, info.request_id, info.retry_after)
    )
    assert "code-secret" not in rendered
    assert "message-secret" not in rendered
    assert "request-secret" not in rendered
    assert "retry-secret" not in rendered


@pytest.mark.parametrize(
    "error_factory",
    [ControlFlowStringError, ControlFlowGetterError],
)
@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
def test_safety_helpers_propagate_control_flow_errors(
    error_factory: type[Exception],
    control_error: BaseException,
) -> None:
    """安全字符串和属性读取辅助必须原样传播控制流异常。

    Args:
        error_factory: 从字符串或 getter 触发控制流的异常类型。
        control_error: 待原样传播的控制流异常。

    Returns:
        None。
    """
    malformed = error_factory(control_error)

    with pytest.raises(type(control_error)) as raised:
        classify_llm_error(malformed)

    assert raised.value is control_error


def test_exploding_exception_type_name_uses_fixed_unknown_fallback() -> None:
    """最终降级不得因异常类型名 getter 失败而再次裸抛。

    Returns:
        None。
    """
    info = classify_llm_error(ExplodingTypeNameError("malformed"))

    assert info.kind is LLMErrorKind.UNKNOWN
    assert info.retryable is False
    assert info.message == "Exception"
    assert info.original_exception_type == "Exception"


def test_safe_traceback_reader_propagates_control_flow_errors() -> None:
    """安全堆栈读取只吞普通 getter 故障，控制流异常必须原样传播。

    Returns:
        None。
    """
    control_error = KeyboardInterrupt()

    class ControlFlowTracebackError(Exception):
        """读取 traceback 时抛出指定控制流异常的测试错误。"""

        @property
        def __traceback__(self) -> object:
            """抛出测试控制流异常。

            Returns:
                此 getter 不返回。
            """
            raise control_error

    malformed = ControlFlowTracebackError("malformed")

    with pytest.raises(KeyboardInterrupt) as raised:
        safe_exception_traceback(malformed)

    assert raised.value is control_error
