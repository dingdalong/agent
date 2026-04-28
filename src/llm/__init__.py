from src.llm.base import LLMProvider, LLMResponse
from src.llm.deepseek import DeepSeekProvider
from src.llm.openai import OpenAIProvider
from llm.anthropic import AnthropicProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}

def get_provider(name: str) -> type[LLMProvider]:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"未知的 LLM provider: {name!r}，可选: {list(_PROVIDERS)}")
    return cls

__all__ = ["LLMProvider", "LLMResponse", "get_provider"]
