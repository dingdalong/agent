import requests
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings

class OllamaEmbeddingFunction(EmbeddingFunction):
    def __init__(self, model_name: str, base_url: str):
        self.model_name = model_name
        self.base_url = base_url.rstrip('/')
        self.api_url = f"{self.base_url}/api/embeddings"

    @classmethod
    def name(cls) -> str:
        return "ollama"

    def get_config(self) -> dict:
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "api_url": self.api_url
        }

    @classmethod
    def build_from_config(cls, config: dict) -> "OllamaEmbeddingFunction":
        return cls(
            model_name=config["model_name"],
            base_url=config["base_url"]
        )

    def __call__(self, input: Documents) -> Embeddings:
        # 确保输入是列表
        if isinstance(input, str):
            input = [input]
        embeddings = []
        for text in input:
            # 对长文本进行简单截断（Ollama 嵌入模型通常有最大 token 限制）
            truncated = text[:2048]  # 简单按字符截断，可根据需要优化
            response = requests.post(
                self.api_url,
                json={"model": self.model_name, "prompt": truncated}
            )
            if response.status_code == 200:
                embedding = response.json()["embedding"]
                embeddings.append(embedding)
            else:
                raise Exception(f"Ollama embedding error: {response.text}")
        return embeddings
