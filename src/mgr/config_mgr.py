from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigManager:
    """全局配置和用户设置管理器。

    Args:
        config_home: 全局配置目录（~/.agent/），包含 config.yaml 和 .env。
        workdir: 用户工作目录（启动时 cwd），用户设置存放在 workdir/.agent/settings.json。
    """

    def __init__(
        self,
        config_home: Path,
        workdir: Path,
    ) -> None:
        self.config_home = Path(config_home)
        self.config_path = self.config_home / "config.yaml"
        self.workdir = Path(workdir)
        self.settings_path = self.workdir / ".agent" / "settings.json"
        self._lock = threading.RLock()
        self._config: dict[str, Any] = self.load_config()
        self._user_settings: dict[str, Any] = self.load_user_settings()

    def load_config(self) -> dict[str, Any]:
        load_dotenv(self.config_home / ".env")
        config: dict[str, Any] = {}
        if self.config_path.exists():
            with self.config_path.open() as f:
                config = yaml.safe_load(f) or {}

        for key, provider in config.get("llm_provider", {}).items():
            for env_suffix, field in (("API_KEY", "api_key"), ("API_URL", "base_url")):
                value = os.environ.get(f"{key.upper()}_{env_suffix}")
                if value is not None:
                    provider[field] = value

        self._config = config
        return config

    def load_user_settings(self) -> dict[str, Any]:
        with self._lock:
            if not self.settings_path.exists():
                return {}

            try:
                settings = json.loads(self.settings_path.read_text())
            except json.JSONDecodeError as exc:
                logger.warning("忽略用户设置文件 %s：JSON 格式无效：%s", self.settings_path, exc)
                return {}

            if not isinstance(settings, dict):
                logger.warning("忽略用户设置文件 %s：顶层必须是对象", self.settings_path)
                return {}

            return settings

    def _get_value(self, source: dict[str, Any], key: str, default: Any = None) -> Any:
        value: Any = source
        for part in key.split("."):
            if not isinstance(value, dict):
                if default is not None:
                    return default
                raise KeyError(key)
            if part not in value:
                if default is not None:
                    return default
                raise KeyError(key)
            value = value[part]
        return value

    def get_config(self, key: str) -> Any:
        return self._get_value(self._config, key)

    def get_user_setting(self, key: str) -> Any:
        with self._lock:
            return self._get_value(self._user_settings, key, default={})

    def _save_user_settings(self, settings: dict[str, Any]) -> None:
        with self._lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
            fd, tmp_name = tempfile.mkstemp(
                dir=self.settings_path.parent,
                prefix=f".{self.settings_path.name}.",
                suffix=".tmp",
                text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                tmp_path.replace(self.settings_path)
            except Exception:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            self._user_settings = settings

    def append_permission_list(self, list_name: str, rule_text: str) -> None:
        """向 permissions 下指定列表追加规则。

        Args:
            list_name: "allow" 或 "deny"。
            rule_text: 规则文本。
        """
        with self._lock:
            settings = self.load_user_settings()
            permissions = settings.get("permissions")
            if not isinstance(permissions, dict):
                permissions = {}
                settings["permissions"] = permissions

            rules = permissions.get(list_name)
            if not isinstance(rules, list):
                rules = []
                permissions[list_name] = rules

            if rule_text not in rules:
                rules.append(rule_text)

            self._save_user_settings(settings)
