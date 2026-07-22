from src.llm.base import LLMCallContext, LLMProvider, LLMResponse
from src.llm.errors import (
    LLMCallError,
    LLMConfigurationError,
    LLMErrorInfo,
    LLMErrorKind,
    LLMStreamResponseError,
    classify_llm_error,
)
from src.llm.retry import RetryConfig, RetryPolicy, calculate_retry_delay
from src.llm.deepseek import DeepSeekProvider
from src.llm.openai import OpenAIProvider
from src.llm.anthropic import AnthropicProvider
from src.llm.ollama import OllamaProvider
from src.llm.moonshot import MoonshotProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "moonshot": MoonshotProvider,
}

def get_provider(name: str) -> type[LLMProvider]:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"未知的 LLM provider: {name!r}，可选: {list(_PROVIDERS)}")
    return cls

__all__ = [
    "LLMCallContext",
    "LLMCallError",
    "LLMConfigurationError",
    "LLMErrorInfo",
    "LLMErrorKind",
    "LLMProvider",
    "LLMResponse",
    "LLMStreamResponseError",
    "RetryConfig",
    "RetryPolicy",
    "calculate_retry_delay",
    "classify_llm_error",
    "get_provider",
]
