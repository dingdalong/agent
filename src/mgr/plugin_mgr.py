"""插件发现管理器 — 统一扫描全局和项目插件目录，按需分发给子系统。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PluginLayer(Enum):
    """插件来源层级。"""
    GLOBAL = "global"
    PROJECT = "project"


@dataclass(frozen=True)
class PluginInfo:
    """已发现的插件元数据。

    Attributes:
        name: 插件目录名，同时用作技能命名空间。
        root: 插件根目录的绝对路径。
        layer: 插件所属层级（全局 / 项目）。
    """
    name: str
    root: Path
    layer: PluginLayer


@dataclass
class PluginMgr:
    """插件发现管理器 — 扫描全局和项目两层 plugins/ 目录，发现所有插件。

    仅负责发现插件目录，不解析插件内部内容（skills、hooks 等），
    由各子系统从 PluginInfo 中自取所需。

    Attributes:
        workdir: 用户工作目录（项目根目录）。
        global_dir: 全局配置目录（~/.agent/），为 None 时跳过全局层。
    """
    workdir: Path
    global_dir: Path | None = None
    _plugins: list[PluginInfo] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._scan()

    def _scan(self) -> None:
        """扫描全局和项目两层插件目录，结果按 全局→项目 排列。"""
        plugins: list[PluginInfo] = []
        if self.global_dir:
            plugins.extend(self._scan_layer(self.global_dir / "plugins", PluginLayer.GLOBAL))
        plugins.extend(self._scan_layer(self.workdir / ".agent" / "plugins", PluginLayer.PROJECT))
        self._plugins = plugins

    def _scan_layer(self, plugins_dir: Path, layer: PluginLayer) -> list[PluginInfo]:
        """扫描单个 plugins/ 目录下的所有插件子目录。

        Args:
            plugins_dir: plugins/ 目录路径。
            layer: 该目录所属层级。

        Returns:
            按目录名排序的 PluginInfo 列表。
        """
        if not plugins_dir.exists():
            return []
        return [
            PluginInfo(name=p.name, root=p, layer=layer)
            for p in sorted(plugins_dir.iterdir())
            if p.is_dir()
        ]

    def plugins(self, *, layer: PluginLayer | None = None) -> list[PluginInfo]:
        """返回已发现的插件列表，可按层级过滤。

        Args:
            layer: 过滤层级，None 时返回全部。

        Returns:
            PluginInfo 列表，扫描顺序（全局在前、项目在后）。
        """
        if layer is None:
            return list(self._plugins)
        return [p for p in self._plugins if p.layer == layer]

    def reload(self) -> None:
        """重新扫描插件目录（/clear 时调用）。"""
        self._scan()
