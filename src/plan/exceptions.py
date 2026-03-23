"""计划模块的自定义异常类"""

from config import PLAN_MAX_RAW_RESPONSE_LENGTH


class PlanError(Exception):
    """计划系统基类异常"""
    pass


class JSONParseError(PlanError):
    """JSON解析失败异常"""
    def __init__(self, message: str, raw_response: str = None):
        super().__init__(message)
        self.raw_response = raw_response

    def __str__(self) -> str:
        base = super().__str__()
        if self.raw_response and len(self.raw_response) < PLAN_MAX_RAW_RESPONSE_LENGTH:
            return f"{base} (原始响应: {self.raw_response})"
        elif self.raw_response:
            return f"{base} (原始响应过长，已截断)"
        return base


class APIGenerationError(PlanError):
    """API生成失败异常"""
    def __init__(self, message: str, api_error: Exception = None):
        super().__init__(message)
        self.api_error = api_error

    def __str__(self) -> str:
        base = super().__str__()
        if self.api_error:
            return f"{base} (API错误: {self.api_error})"
        return base


class DependencyError(PlanError):
    """依赖关系错误异常"""
    def __init__(self, message: str, step_id: str = None, missing_deps: list = None):
        super().__init__(message)
        self.step_id = step_id
        self.missing_deps = missing_deps or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.step_id:
            if self.missing_deps:
                return f"{base} (步骤: {self.step_id}, 缺失依赖: {self.missing_deps})"
            return f"{base} (步骤: {self.step_id})"
        return base


class VariableResolutionError(PlanError):
    """变量解析失败异常"""
    def __init__(self, message: str, variable_path: str = None, context_keys: list = None):
        super().__init__(message)
        self.variable_path = variable_path
        self.context_keys = context_keys or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.variable_path:
            if self.context_keys:
                return f"{base} (变量路径: {self.variable_path}, 可用上下文: {self.context_keys})"
            return f"{base} (变量路径: {self.variable_path})"
        return base


class StepExecutionError(PlanError):
    """步骤执行失败异常"""
    def __init__(self, message: str, step_id: str = None, step_description: str = None, action: str = None):
        super().__init__(message)
        self.step_id = step_id
        self.step_description = step_description
        self.action = action

    def __str__(self) -> str:
        base = super().__str__()
        parts = []
        if self.step_id:
            parts.append(f"步骤ID: {self.step_id}")
        if self.step_description:
            parts.append(f"描述: {self.step_description}")
        if self.action:
            parts.append(f"动作: {self.action}")
        if parts:
            return f"{base} ({', '.join(parts)})"
        return base


class PlanValidationError(PlanError):
    """计划验证失败异常"""
    def __init__(self, message: str, validation_errors: list = None):
        super().__init__(message)
        self.validation_errors = validation_errors or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.validation_errors:
            errors_str = "; ".join(self.validation_errors)
            return f"{base} (验证错误: {errors_str})"
        return base