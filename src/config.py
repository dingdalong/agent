import yaml
import os
from dotenv import load_dotenv
from pathlib import Path

def load_config(ptch: str = "config.yaml") -> dict:
    load_dotenv()
    config_path = Path(ptch)
    config: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

    for key, provider in config.get("llm_provider", {}).items():
        env_key = f"{key.upper()}_API_KEY"
        value = os.environ.get(env_key)
        if value is not None:
            provider["api_key"] = value

    return config

config = load_config()
