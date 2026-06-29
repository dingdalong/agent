"""角色管理器 — 发现、解析、暴露当前激活角色的路径。

三层发现（低→高）：内置 src/roles/ → 全局 ~/.agent/roles/ → 项目 .agent/roles/。
激活角色由 config.yaml 的 role: 键指定，缺省回退 coding。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from src.mgr.paths import builtin_root, common_role_dir

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager

logger = logging.getLogger(__name__)

# 缺省角色名 — 当 config 未指定或指定的角色不存在时回退到此角色。
_DEFAULT_ROLE = "coding"


@dataclass
class RoleMgr:
    """角色管理器。

    Args:
        config_mgr: 配置管理器，用于读取 role: 键。
        workdir: 用户工作目录。
        global_dir: 全局配置目录（~/.agent/），为 None 时跳过全局层。
    """

    config_mgr: ConfigManager
    workdir: Path
    global_dir: Path | None = None

    _role_path: Path | None = field(init=False, default=None)
    _role_config: dict[str, Any] = field(init=False, default_factory=dict)
    _all_roles: dict[str, Path] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._discover()
        self._resolve()

    # —— 发现 ————————————————————————————————————————————————————————

    def _discover(self) -> None:
        """三层扫描发现所有已安装角色，同名后者覆盖。

        扫描顺序（低→高优先级）：内置 → 全局 → 项目。
        """
        scan_dirs: list[Path] = [builtin_root() / "roles"]
        if self.global_dir:
            scan_dirs.append(self.global_dir / "roles")
        scan_dirs.append(self.workdir / ".agent" / "roles")

        for directory in scan_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_dir():
                    continue
                role_yaml = path / "role.yaml"
                if not role_yaml.exists():
                    continue
                name = self._read_role_name(role_yaml) or path.name
                self._all_roles[name] = path

    @staticmethod
    def _read_role_name(path: Path) -> str | None:
        """从 role.yaml 中仅提取 name 字段，不完整解析。

        Args:
            path: role.yaml 文件路径。

        Returns:
            name 字段值，解析失败或缺失时返回 None。
        """
        try:
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict):
                name = data.get("name")
                if isinstance(name, str) and name:
                    return name
        except yaml.YAMLError:
            pass
        return None

    # —— 解析 ————————————————————————————————————————————————————————

    def _resolve(self) -> None:
        """从配置读取角色名，定位角色目录并解析 role.yaml。

        若配置不存在或无值 → 回退 _DEFAULT_ROLE。
        若指定角色在 _all_roles 中不存在 → warning + 回退。
        """
        role_name: str | None = None
        try:
            val = self.config_mgr.get_config("role")
            if isinstance(val, str) and val.strip():
                role_name = val.strip()
        except KeyError:
            pass

        if not role_name:
            role_name = _DEFAULT_ROLE

        path = self._all_roles.get(role_name)
        if path is None:
            if role_name != _DEFAULT_ROLE:
                logger.warning(
                    "角色 '%s' 未找到，回退到 '%s'。可用角色：%s",
                    role_name,
                    _DEFAULT_ROLE,
                    ", ".join(sorted(self._all_roles)) or "(无)",
                )
            path = self._all_roles.get(_DEFAULT_ROLE)

        if path is None:
            logger.warning("默认角色 '%s' 也未找到，无角色激活。", _DEFAULT_ROLE)
            self._role_path = None
            self._role_config = {}
            return

        self._role_path = path
        self._role_config = self._parse_role_yaml(path / "role.yaml")
        logger.info("激活角色：%s（%s）", self.role_name, path)

    @staticmethod
    def _parse_role_yaml(path: Path) -> dict[str, Any]:
        """解析 role.yaml 返回 dict，文件不存在或格式错误返回空 dict。

        Args:
            path: role.yaml 文件路径。

        Returns:
            解析后的配置 dict。
        """
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text())
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError as exc:
            logger.warning("角色配置 %s 解析失败：%s", path, exc)
            return {}

    # —— 查询 ————————————————————————————————————————————————————————

    @property
    def active(self) -> bool:
        """是否有已激活的角色。"""
        return self._role_path is not None

    @property
    def role_name(self) -> str | None:
        """当前角色名。"""
        if not self._role_path:
            return None
        name = self._role_config.get("name")
        if isinstance(name, str) and name:
            return name
        return self._role_path.name

    @property
    def identity(self) -> str | None:
        """主 agent 人设文本。空字符串 → None（回退通用身份）。"""
        val = self._role_config.get("identity")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    @property
    def description(self) -> str:
        """角色的一行描述。"""
        val = self._role_config.get("description")
        return str(val) if val else ""

    # —— 资产路径（无角色时返回 None）——————————————————————————————————

    def _make_path(self, sub: str) -> Path | None:
        """构造角色子目录路径，仅目录存在时返回。

        Args:
            sub: 角色目录内的相对子路径。

        Returns:
            Path 或 None。
        """
        if not self._role_path:
            return None
        p = self._role_path / sub
        return p if p.exists() else None

    def agents_dir(self) -> Path | None:
        """角色子 agent 定义目录（*.md）。"""
        return self._make_path("agents")

    def skills_dir(self) -> Path | None:
        """角色技能目录（*/SKILL.md）。"""
        return self._make_path("skills")

    def plugins_dir(self) -> Path | None:
        """角色插件目录。"""
        return self._make_path("plugins")

    def agent_md_path(self) -> Path | None:
        """角色 AGENT.md 文件路径。"""
        if not self._role_path:
            return None
        p = self._role_path / "AGENT.md"
        return p if p.is_file() else None

    def mcp_servers_path(self) -> Path | None:
        """角色 mcp_servers.json 文件路径。"""
        if not self._role_path:
            return None
        p = self._role_path / "mcp_servers.json"
        return p if p.is_file() else None

    # —— 共享资源路径（不依赖角色激活状态）——————————————————————————————

    def common_dir(self) -> Path | None:
        """共享资源目录（所有角色可用）。"""
        p = common_role_dir()
        return p if p.exists() else None

    def common_agents_dir(self) -> Path | None:
        """共享子 agent 定义目录。"""
        p = common_role_dir() / "agents"
        return p if p.exists() else None

    def common_skills_dir(self) -> Path | None:
        """共享技能目录。"""
        p = common_role_dir() / "skills"
        return p if p.exists() else None

    def common_agent_md_path(self) -> Path | None:
        """共享 AGENT.md 文件。"""
        p = common_role_dir() / "AGENT.md"
        return p if p.is_file() else None
