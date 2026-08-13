"""三层配置管理器 — 内置默认 → 全局 (~/.agent/) → 项目 ({cwd}/.agent/)，逐层深度合并。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from dotenv import dotenv_values, set_key

from src.mgr.paths import builtin_root
from src.mgr.secure_io import atomic_write_text

logger = logging.getLogger(__name__)

ConfigScope = Literal["global", "project"]

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        logger.warning("忽略配置文件 %s：YAML 格式无效：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("忽略配置文件 %s：顶层必须是对象", path)
        return {}
    return data


def _load_writable_yaml(path: Path) -> dict[str, Any]:
    """加载待回写的 YAML 配置，异常内容不能被覆盖。"""
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件 {path} 的 YAML 格式无效") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {path} 的顶层必须是对象")
    return data


def _user_config_has_provider(path: Path) -> bool:
    """只读检测单个用户层 config.yaml 是否含显式 llm_provider，异常内容保守视为显式。

    与 ``_load_yaml`` 的容错语义不同：YAML 无效或顶层非对象时不降级为空，
    而是视为显式配置，避免向导覆盖无法解析的用户内容。

    Args:
        path: 用户层 config.yaml 路径。

    Returns:
        ``llm_provider`` 为非空 mapping（含未知/不完整段）时为 True；
        值为非 mapping（含显式 null）或文件 YAML 无效/顶层非对象时保守返回 True；
        文件不存在、无 ``llm_provider`` 键或值为空 mapping 时返回 False。
    """
    if not path.exists():
        return False
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError:
        return True
    if not isinstance(data, dict):
        return True
    if "llm_provider" not in data:
        return False
    provider = data["llm_provider"]
    if provider is None:
        # 键存在但值为显式 null：属非 mapping，保守视为显式，避免向导覆盖用户内容。
        return True
    if not isinstance(provider, dict):
        return True
    return bool(provider)


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
        project_trusted: bool = False,
    ) -> None:
        self.global_dir = Path(global_dir)
        self.workdir = Path(workdir)
        self.project_dir = self.workdir / ".agent"
        self.project_trusted = project_trusted
        self.global_config_path = self.global_dir / "config.yaml"
        self.project_config_path = self.project_dir / "config.yaml"
        # settings.json 写入位置固定为项目级
        self.settings_path = self.project_dir / "settings.json"
        self._lock = threading.RLock()
        self._environment: dict[str, str] = {}
        self._config: dict[str, Any] = self.load_config()
        self._user_settings: dict[str, Any] = self.load_user_settings()

    def reload(self) -> None:
        """重新加载配置（/clear 时调用）。"""
        with self._lock:
            self._config = self.load_config()
            self._user_settings = self.load_user_settings()

    def set_project_trusted(self, trusted: bool) -> None:
        self.project_trusted = trusted
        self.reload()

    def load_config(self) -> dict[str, Any]:
        """加载三层配置并深度合并，同时构造私有有效环境。

        Returns:
            合并后的配置 dict。
        """
        # .env 加载优先级（低→高）：
        # 全局 ~/.agent/.env → 仓库根 {workdir}/.env → 项目 {workdir}/.agent/.env
        # 仓库根 .env 是最常见的放置位置，纳入加载以避免配置读取不到。
        global_env = self.global_dir / ".env"
        root_env = self.workdir / ".env"
        project_env = self.project_dir / ".env"
        env_paths = (global_env, root_env, project_env) if self.project_trusted else (global_env,)
        environment = {str(key): str(value) for key, value in os.environ.items()}
        for env_path in env_paths:
            try:
                values = dotenv_values(env_path)
            except (OSError, ValueError):
                continue
            environment.update(
                {str(key): str(value) for key, value in values.items() if value is not None}
            )
        self._environment = environment

        # 三层 config.yaml 深度合并
        builtin_config = _load_yaml(builtin_root() / "config.yaml")
        global_config = _load_yaml(self.global_config_path)
        project_config = _load_yaml(self.project_config_path)
        if not self.project_trusted:
            project_config.pop("llm_provider", None)
            project_config.pop("llm", None)

        config = _deep_merge(builtin_config, global_config)
        config = _deep_merge(config, project_config)

        # 环境变量覆盖 provider 的 API key 和 base_url；llm_provider 非 mapping 时
        # 跳过覆盖，交由后续 LLMMgr 校验报错
        providers = config.get("llm_provider", {})
        if not isinstance(providers, dict):
            providers = {}
        for key, provider in providers.items():
            for env_suffix, field in (("API_KEY", "api_key"), ("API_URL", "base_url")):
                value = environment.get(f"{key.upper()}_{env_suffix}")
                if value is not None:
                    provider[field] = value

        self._config = config
        return config

    @property
    def environment(self) -> dict[str, str]:
        """返回本次信任状态对应的私有有效环境副本。"""
        return dict(self._environment)

    def has_explicit_provider_config(self) -> bool:
        """判断用户层是否已存在显式 LLM Provider 配置。

        显式配置来源（任一命中即 True）：
        - 有效环境中存在任一内置 Provider 的 ``{NAME}_API_KEY`` 或 ``{NAME}_API_URL`` 键
          （键存在即算，包括值为空字符串）；有效环境遵循本次信任边界；
        - 全局 config.yaml 存在非空 ``llm_provider`` mapping；项目 trusted 时项目
          config.yaml 同规则，未信任项目忽略；
        - 用户层 ``llm_provider`` 非 mapping 或 YAML 无效时保守视为显式，避免向导覆盖。
        内置 ``src/config.yaml`` 的 ``llm_provider`` 与用户层仅配置 ``llm.default``
        均不视为显式配置。

        Returns:
            是否存在显式 LLM Provider 配置。
        """
        if _user_config_has_provider(self.global_config_path):
            return True
        if self.project_trusted and _user_config_has_provider(self.project_config_path):
            return True
        builtin_providers = _load_yaml(builtin_root() / "config.yaml").get("llm_provider", {})
        if not isinstance(builtin_providers, dict):
            builtin_providers = {}
        for name in builtin_providers:
            if (
                f"{name.upper()}_API_KEY" in self._environment
                or f"{name.upper()}_API_URL" in self._environment
            ):
                return True
        return False

    def load_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """读取并合并两层 mcp_servers.json，返回 {server_name: spec}。

        优先级（低→高）：全局 ~/.agent/mcp_servers.json → 项目 {workdir}/mcp_servers.json。
        仅读取顶层 mcpServers 字段，同名 server 由项目层覆盖全局层。

        Returns:
            合并后的 MCP server 配置字典，键为 server 名，值为该 server 的连接配置。
        """
        global_servers = _load_json(self.global_dir / "mcp_servers.json").get("mcpServers", {})
        project_servers = (
            _load_json(self.workdir / ".agent" / "mcp_servers.json").get("mcpServers", {})
            if self.project_trusted else {}
        )
        if not isinstance(global_servers, dict):
            global_servers = {}
        if not isinstance(project_servers, dict):
            project_servers = {}
        return _deep_merge(global_servers, project_servers)

    def load_user_settings(self) -> dict[str, Any]:
        """加载双层 settings.json；受限模式忽略项目 Hook 配置。

        Returns:
            合并后的用户设置 dict。
        """
        with self._lock:
            global_settings = _load_json(self.global_dir / "settings.json")
            project_settings = _load_json(self.settings_path) if self.project_trusted else {}

            if not global_settings:
                return project_settings
            if not project_settings:
                return global_settings

            return _deep_merge(global_settings, project_settings)

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

    def _config_path(self, scope: ConfigScope) -> Path:
        """返回可回写配置层的目标路径。"""
        if scope == "global":
            return self.global_config_path
        if scope == "project":
            return self.project_config_path
        raise ValueError(f"不支持的配置层: {scope!r}")

    def set_config(self, key: str, value: Any, scope: ConfigScope) -> None:
        """将 YAML 可序列化值写入指定配置层的点路径。

        写入只修改目标层自身，不会写入内置配置或合并后的有效配置。
        新值在下次 ``reload()`` 或重启后生效。

        Args:
            key: 非空点分隔配置路径，如 ``"llm.default"``。
            value: YAML 可序列化的配置值。
            scope: 目标配置层，仅支持 ``"global"`` 或 ``"project"``。
        """
        if not isinstance(key, str) or not key or any(not part for part in key.split(".")):
            raise ValueError(f"无效配置路径: {key!r}")

        self.set_configs({key: value}, scope)

    def set_configs(self, values: Mapping[str, Any], scope: ConfigScope) -> None:
        """原子地将多个 YAML 值写入同一配置层。"""
        if not values:
            return
        for key in values:
            if not isinstance(key, str) or not key or any(not part for part in key.split(".")):
                raise ValueError(f"无效配置路径: {key!r}")

        with self._lock:
            path = self._config_path(scope)
            config = _load_writable_yaml(path)
            for key, value in values.items():
                parts = key.split(".")
                target = config
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    elif not isinstance(target[part], dict):
                        raise ValueError(f"配置路径 {key!r} 的中间节点 {part!r} 必须是对象")
                    target = target[part]
                target[parts[-1]] = value

            content = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).rstrip() + "\n"
            atomic_write_text(path, content)

    def set_global_env(self, values: Mapping[str, str]) -> None:
        """批量原子写入全局 .env（``self.global_dir / ".env"``）。

        - values 为空时直接返回；变量名必须匹配 ``[A-Za-z_][A-Za-z0-9_]*``，
          值必须是 str，非法输入在任何文件操作前抛 ValueError/TypeError。
        - 锁定完整读取 → staging → 最终替换流程；保留注释、export、无关变量与
          有效 dotenv 原文。
        - 通过 python-dotenv 的 ``set_key(..., quote_mode="always")`` 在 owner-only
          staging 文件上依次应用目标变量，最后一次性 ``atomic_write_text`` 原子替换
          目标（目录 0700、文件 0600）。异常时原目标不变且 staging 清理。
        - 不修改 os.environ，不自动 reload，新值在下次 ``reload()`` 或重启后生效
          （与 set_configs 契约一致）。

        Args:
            values: 目标环境变量 mapping，键为 env key，值为 str。
        """
        if not values:
            return
        for key, value in values.items():
            if not isinstance(key, str) or _ENV_KEY_RE.fullmatch(key) is None:
                raise ValueError(f"无效环境变量名: {key!r}")
            if not isinstance(value, str):
                raise TypeError(f"环境变量 {key!r} 的值必须是 str，得到 {type(value).__name__}")

        with self._lock:
            target = self.global_dir / ".env"
            self.global_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.global_dir, 0o700)
            except OSError:
                pass
            fd, tmp_name = tempfile.mkstemp(dir=self.global_dir, prefix=f".{target.name}.")
            os.close(fd)
            staging = Path(tmp_name)
            try:
                if target.exists():
                    shutil.copyfile(target, staging)
                for key, value in values.items():
                    set_key(staging, key, value, quote_mode="always")
                atomic_write_text(target, staging.read_text(encoding="utf-8"))
            finally:
                staging.unlink(missing_ok=True)

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
            content = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
            atomic_write_text(self.settings_path, content)
            self._user_settings = settings
