import yaml
import os
from dotenv import load_dotenv
from pathlib import Path

def override(config: dict, key: str, env_key: str):
    value = os.environ.get(env_key)
    if value is not None:
        config[key] = value

def load_config(ptch: str = "config.yaml") -> dict:
    load_dotenv()
    config_path = Path(ptch)
    config: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

    override(config["llm_provider"]["deepseek"], "api_key", "DEEPSEEK_API_KEY")

    return config

config = load_config()
