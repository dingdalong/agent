"""三层配置管理器 — 内置默认 → 全局 (~/.agent/) → 项目 ({cwd}/.agent/)，逐层深度合并。"""

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

from src.mgr.paths import builtin_root

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归深度合并两个 dict，override 中的值覆盖 base 中的同名键。

    Args:
        base: 基础配置。
        override: 覆盖配置。

    Returns:
        合并后的新 dict。
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """加载 YAML 文件，不存在或解析失败时返回空 dict。

    Args:
        path: YAML 文件路径。

    Returns:
        解析后的 dict。
    """
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        logger.warning("忽略配置文件 %s：YAML 格式无效：%s", path, exc)
        return {}


def _load_json(path: Path) -> dict[str, Any]:
    """加载 JSON 文件，不存在或解析失败时返回空 dict。

    Args:
        path: JSON 文件路径。

    Returns:
        解析后的 dict。
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        logger.warning("忽略设置文件 %s：JSON 格式无效：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("忽略设置文件 %s：顶层必须是对象", path)
        return {}
    return data


class ConfigManager:
    """三层配置管理器。

    配置合并优先级（低→高）：内置默认 → 全局 → 项目。

    Args:
        global_dir: 全局配置目录（~/.agent/）。
        workdir: 用户工作目录（启动时 cwd）。
    """

    def __init__(
        self,
        global_dir: Path,
        workdir: Path,
    ) -> None:
        self.global_dir = Path(global_dir)
        self.workdir = Path(workdir)
        self.project_dir = self.workdir / ".agent"
        # settings.json 写入位置固定为项目级
        self.settings_path = self.project_dir / "settings.json"
        self._lock = threading.RLock()
        self._config: dict[str, Any] = self.load_config()
        self._user_settings: dict[str, Any] = self.load_user_settings()

    def reload(self) -> None:
        """重新加载配置（/clear 时调用）。"""
        with self._lock:
            self._config = self.load_config()
            self._user_settings = self.load_user_settings()

    def load_config(self) -> dict[str, Any]:
        """加载三层配置并深度合并，同时加载三层 .env。

        Returns:
            合并后的配置 dict。
        """
        # .env 加载优先级（低→高，override=True 后加载者覆盖同名变量）：
        # 全局 ~/.agent/.env → 仓库根 {workdir}/.env → 项目 {workdir}/.agent/.env
        # 仓库根 .env 是最常见的放置位置，纳入加载以避免配置读取不到。
        global_env = self.global_dir / ".env"
        root_env = self.workdir / ".env"
        project_env = self.project_dir / ".env"
        for env_path in (global_env, root_env, project_env):
            if env_path.exists():
                load_dotenv(env_path, override=True)

        # 三层 config.yaml 深度合并
        builtin_config = _load_yaml(builtin_root() / "config.yaml")
        global_config = _load_yaml(self.global_dir / "config.yaml")
        project_config = _load_yaml(self.project_dir / "config.yaml")

        config = _deep_merge(builtin_config, global_config)
        config = _deep_merge(config, project_config)

        # 环境变量覆盖 provider 的 API key 和 base_url
        for key, provider in config.get("llm_provider", {}).items():
            for env_suffix, field in (("API_KEY", "api_key"), ("API_URL", "base_url")):
                value = os.environ.get(f"{key.upper()}_{env_suffix}")
                if value is not None:
                    provider[field] = value

        self._config = config
        return config

    def load_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """读取并合并两层 mcp_servers.json，返回 {server_name: spec}。

        优先级（低→高）：全局 ~/.agent/mcp_servers.json → 项目 {workdir}/mcp_servers.json。
        仅读取顶层 mcpServers 字段，同名 server 由项目层覆盖全局层。

        Returns:
            合并后的 MCP server 配置字典，键为 server 名，值为该 server 的连接配置。
        """
        global_servers = _load_json(self.global_dir / "mcp_servers.json").get("mcpServers", {})
        project_servers = _load_json(self.workdir / ".agent" / "mcp_servers.json").get("mcpServers", {})
        if not isinstance(global_servers, dict):
            global_servers = {}
        if not isinstance(project_servers, dict):
            project_servers = {}
        return _deep_merge(global_servers, project_servers)

    def load_user_settings(self) -> dict[str, Any]:
        """加载双层 settings.json 并合并 permissions 列表。

        全局和项目的 allow/deny 列表合并（去重），其余字段项目层覆盖全局层。

        Returns:
            合并后的用户设置 dict。
        """
        with self._lock:
            global_settings = _load_json(self.global_dir / "settings.json")
            project_settings = _load_json(self.settings_path)

            if not global_settings:
                return project_settings
            if not project_settings:
                return global_settings

            merged = _deep_merge(global_settings, project_settings)

            # permissions.allow 和 permissions.deny 合并去重
            global_perms = global_settings.get("permissions", {})
            project_perms = project_settings.get("permissions", {})
            if global_perms or project_perms:
                merged_perms = merged.setdefault("permissions", {})
                for list_name in ("allow", "deny"):
                    global_list = global_perms.get(list_name, [])
                    project_list = project_perms.get(list_name, [])
                    if isinstance(global_list, list) and isinstance(project_list, list):
                        seen = set()
                        combined = []
                        for item in global_list + project_list:
                            if item not in seen:
                                seen.add(item)
                                combined.append(item)
                        merged_perms[list_name] = combined

            return merged

    def _get_value(self, source: dict[str, Any], key: str, default: Any = None) -> Any:
        """按点分隔路径从嵌套 dict 中取值。

        Args:
            source: 数据源 dict。
            key: 点分隔的键路径，如 "llm.default.model"。
            default: 键不存在时的默认值，为 None 时抛 KeyError。

        Returns:
            找到的值，或 default。
        """
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
        """获取配置值。

        Args:
            key: 点分隔的键路径。

        Returns:
            配置值。
        """
        return self._get_value(self._config, key)

    def get_user_setting(self, key: str) -> Any:
        """获取用户设置值。

        Args:
            key: 点分隔的键路径。

        Returns:
            设置值，不存在时返回空 dict。
        """
        with self._lock:
            return self._get_value(self._user_settings, key, default={})

    def _save_user_settings(self, settings: dict[str, Any]) -> None:
        """原子写入项目级 settings.json。

        Args:
            settings: 要写入的完整设置 dict。
        """
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
        """向项目级 permissions 下指定列表追加规则。

        Args:
            list_name: "allow" 或 "deny"。
            rule_text: 规则文本。
        """
        with self._lock:
            settings = _load_json(self.settings_path)
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
