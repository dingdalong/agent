"""计划模块的自定义异常类"""

_MAX_RAW_RESPONSE_LENGTH = 500


class PlanError(Exception):
    """计划系统基类异常"""
    pass


class JSONParseError(PlanError):
    """JSON 解析失败"""

    def __init__(self, message: str, raw_response: str = None):
        super().__init__(message)
        self.raw_response = raw_response

    def __str__(self) -> str:
        base = super().__str__()
        if self.raw_response and len(self.raw_response) < _MAX_RAW_RESPONSE_LENGTH:
            return f"{base} (原始响应: {self.raw_response})"
        elif self.raw_response:
            return f"{base} (原始响应过长，已截断)"
        return base


class APIGenerationError(PlanError):
    """API 生成失败"""

    def __init__(self, message: str, api_error: Exception = None):
        super().__init__(message)
        self.api_error = api_error

    def __str__(self) -> str:
        base = super().__str__()
        if self.api_error:
            return f"{base} (API错误: {self.api_error})"
        return base


class CompileError(PlanError):
    """Plan → CompiledGraph 编译失败"""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.details:
            return f"{base} ({'; '.join(self.details)})"
        return base
