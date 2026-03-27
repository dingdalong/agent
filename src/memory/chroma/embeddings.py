"""Embedding 客户端，使用 requests.Session 复用连接。"""

import logging

import requests
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

logger = logging.getLogger(__name__)


class EmbeddingClient(EmbeddingFunction):
    """基于 Ollama API 的 embedding 函数，带连接池。"""

    def __init__(self, model_name: str, base_url: str, max_chars: int = 2048):
        self._model = model_name
        self._base_url = base_url.rstrip("/")
        self._url = f"{self._base_url}/api/embeddings"
        self._max_chars = max_chars
        self._session = requests.Session()

    @classmethod
    def name(cls) -> str:
        return "embedding_client"

    def get_config(self) -> dict:
        return {
            "model_name": self._model,
            "base_url": self._base_url,
            "api_url": self._url,
        }

    @classmethod
    def build_from_config(cls, config: dict) -> "EmbeddingClient":
        return cls(model_name=config["model_name"], base_url=config["base_url"])

    def __call__(self, input: Documents) -> Embeddings:
        if isinstance(input, str):
            input = [input]
        embeddings = []
        for text in input:
            truncated = self._safe_truncate(text, self._max_chars)
            resp = self._session.post(
                self._url,
                json={"model": self._model, "prompt": truncated},
            )
            resp.raise_for_status()
            embeddings.append(resp.json()["embedding"])
        return embeddings

    @staticmethod
    def _safe_truncate(text: str, max_chars: int) -> str:
        """安全截断，不破坏多字节 UTF-8 字符。"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars]
