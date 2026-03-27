"""配置加载器 — 读 config.yaml + .env，返回原始 dict。"""

import os

import yaml
from dotenv import load_dotenv
from pathlib import Path


def load_config(path: str = "config.yaml") -> dict:
    """加载配置文件，返回原始 dict。文件不存在返回空 dict。"""
    load_dotenv()
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    # .env 中的 secrets 合并到 config
    if "llm" not in config:
        config["llm"] = {}
    if not config["llm"].get("api_key"):
        config["llm"]["api_key"] = os.getenv("OPENAI_API_KEY", "")
    if not config["llm"].get("base_url"):
        config["llm"]["base_url"] = os.getenv("OPENAI_BASE_URL", "")
    if not config["llm"].get("model"):
        config["llm"]["model"] = os.getenv("OPENAI_MODEL", "")

    if "embedding" not in config:
        config["embedding"] = {}
    if not config["embedding"].get("model"):
        config["embedding"]["model"] = os.getenv("OPENAI_MODEL_EMBEDDING", "")
    if not config["embedding"].get("base_url"):
        config["embedding"]["base_url"] = os.getenv("OPENAI_MODEL_EMBEDDING_URL", "")

    if "user" not in config:
        config["user"] = {}
    if not config["user"].get("id"):
        config["user"]["id"] = os.getenv("USER_ID", "default_user")

    return config
