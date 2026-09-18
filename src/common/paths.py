"""路径解析 — 集中管理三层目录体系（内置 → 全局 → 项目）。"""

from __future__ import annotations

import os
from pathlib import Path


def builtin_root() -> Path:
    """返回内置资源根目录。

    debug 时指向 src/ 源码目录，安装后指向 site-packages 中的包目录。
    内置 config.yaml、skills/、agent/agents/ 等资源均在此目录下。

    Returns:
        内置资源根目录的 Path 对象。
    """
    return Path(__file__).resolve().parent.parent


def common_role_dir() -> Path:
    """返回共享角色资源目录（src/roles/common/）。

    该目录不是可激活的角色（无 role.md），存放所有角色共享的
    agents、skills、AGENTS.md 等资产。在加载优先级中处于最低层。

    Returns:
        共享角色资源目录的 Path 对象。
    """
    return builtin_root() / "roles" / "common"


def global_data_dir() -> Path:
    """返回全局配置目录。

    优先使用环境变量 $AGENT_HOME，否则默认 ~/.agent/。
    存放用户跨项目的配置、skills、agents、plugins 等。

    Returns:
        全局配置目录的 Path 对象。
    """
    return Path(os.environ.get("AGENT_HOME", Path.home() / ".agent"))


def project_data_dir(workdir: Path) -> Path:
    """返回项目配置目录。

    存放项目特有的配置、skills、agents、plugins、memory、plans 等。

    Args:
        workdir: 用户工作目录。

    Returns:
        项目配置目录的 Path 对象（{workdir}/.agent/）。
    """
    return workdir / ".agent"


def workdir(override: str | None = None) -> Path:
    """返回用户工作目录。

    优先使用 override 参数，其次环境变量 $AGENT_WORKDIR，最后 Path.cwd()。

    Args:
        override: 命令行传入的工作目录覆盖值。

    Returns:
        用户工作目录的 Path 对象。
    """
    if override:
        return Path(override).resolve()
    env = os.environ.get("AGENT_WORKDIR")
    if env:
        return Path(env).resolve()
    return Path.cwd()
