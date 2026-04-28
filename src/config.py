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
        for env_suffix, field in (("API_KEY", "api_key"), ("API_URL", "base_url")):
            value = os.environ.get(f"{key.upper()}_{env_suffix}")
            if value is not None:
                provider[field] = value

    return config

config = load_config()
