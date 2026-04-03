"""配置加载器 — 读 config.yaml + .env，返回 AppConfig。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """应用配置，持有解析后的绝对路径和原始配置字典。

    Attributes:
        workspace: 用户项目上下文目录（绝对路径）。
        data_dir: 系统数据目录（绝对路径）。
        raw: 原始 config dict，下游组件按需读取非路径配置。
    """

    workspace: Path
    data_dir: Path
    raw: dict = field(default_factory=dict)

    def resolve(self, relative: str | Path) -> Path:
        """相对于 workspace 解析路径，绝对路径原样返回。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.workspace / p).resolve()

    def resolve_data(self, relative: str | Path) -> Path:
        """相对于 data_dir 解析路径，绝对路径原样返回。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.data_dir / p).resolve()


def load_config(path: str = "config.yaml") -> AppConfig:
    """加载配置文件，返回 AppConfig。文件不存在时使用空配置。"""
    load_dotenv()
    config_path = Path(path)
    raw: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    # .env 中的 secrets 合并到 raw
    if "llm" not in raw:
        raw["llm"] = {}
    if not raw["llm"].get("api_key"):
        raw["llm"]["api_key"] = os.getenv("OPENAI_API_KEY", "")
    if not raw["llm"].get("base_url"):
        raw["llm"]["base_url"] = os.getenv("OPENAI_BASE_URL", "")
    if not raw["llm"].get("model"):
        raw["llm"]["model"] = os.getenv("OPENAI_MODEL", "")

    if "embedding" not in raw:
        raw["embedding"] = {}
    if not raw["embedding"].get("model"):
        raw["embedding"]["model"] = os.getenv("OPENAI_MODEL_EMBEDDING", "")
    if not raw["embedding"].get("base_url"):
        raw["embedding"]["base_url"] = os.getenv("OPENAI_MODEL_EMBEDDING_URL", "")

    if "user" not in raw:
        raw["user"] = {}
    if not raw["user"].get("id"):
        raw["user"]["id"] = os.getenv("USER_ID", "default_user")

    # 路径解析：workspace 相对于 config.yaml 所在目录
    config_dir = config_path.resolve().parent
    workspace = (config_dir / raw.get("workspace", ".")).resolve()
    data_dir = (workspace / raw.get("data_dir", ".agent_data")).resolve()

    return AppConfig(workspace=workspace, data_dir=data_dir, raw=raw)
