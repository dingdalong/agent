"""AGENTS.md 提示词加载与角色作用域测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.tools  # noqa: F401  先完成内置工具注册，避免 mgr 聚合包初始化循环
from src.mgr.paths import builtin_root
from src.mgr.prompt_mgr import PromptMgr
from src.mgr.role_mgr import RoleMgr, extract_manifest, parse_frontmatter


class _RoleMgrStub:
    """提供 PromptMgr 所需角色资源路径的最小桩。"""

    def __init__(self, common_path: Path | None, role_path: Path | None) -> None:
        """保存共享层和激活角色层的准则文件路径。

        Args:
            common_path: 共享 AGENTS.md 路径。
            role_path: 激活角色 AGENTS.md 路径。
        """
        self.active = True
        self._common_path = common_path
        self._role_path = role_path

    def common_agent_md_path(self) -> Path | None:
        """返回共享 AGENTS.md 路径。

        Returns:
            共享准则文件路径。
        """
        return self._common_path

    def agent_md_path(self) -> Path | None:
        """返回激活角色 AGENTS.md 路径。

        Returns:
            角色共享准则文件路径。
        """
        return self._role_path


def _build_prompt(
    *,
    is_subagent: bool,
    role_mgr: _RoleMgrStub,
    workdir: Path,
    global_dir: Path,
    role_prompt: str,
) -> str:
    """构建最小主/子 agent 的 system prompt。

    Args:
        is_subagent: 是否模拟子 agent。
        role_mgr: 提供行为准则路径的角色管理器桩。
        workdir: 项目层目录。
        global_dir: 用户全局层目录。
        role_prompt: 当前 agent 的核心身份提示词。

    Returns:
        生成的 system prompt 正文。
    """
    agent = SimpleNamespace(
        deps=SimpleNamespace(
            role_mgr=role_mgr,
            memory_mgr=None,
            session_context=[],
        ),
        is_subagent=is_subagent,
        memory=None,
    )
    prompt_mgr = PromptMgr(
        agent=agent,
        model="test-model",
        workdir=workdir,
        global_dir=global_dir,
        role_prompt=role_prompt,
    )
    return prompt_mgr.build()[0]["content"]


def _load_manifest_prompt(path: Path, default_id: str) -> str:
    """解析 role.md 或子 agent 定义中的提示词 body。

    Args:
        path: role.md 或 agents/*.md 文件路径。
        default_id: frontmatter 缺少 agent_type 时使用的标识。

    Returns:
        解析出的非空提示词正文。
    """
    metadata, body = parse_frontmatter(path.read_text())
    manifest = extract_manifest(
        metadata,
        path,
        prompt=body,
        id_field="agent_type",
        default_id=default_id,
        default_description="",
    )
    assert manifest.prompt is not None
    return manifest.prompt


def test_role_manager_uses_agents_md_and_ignores_legacy_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RoleMgr 的角色与 common 路径只识别 AGENTS.md。"""
    role_dir = tmp_path / "role"
    common_dir = tmp_path / "common"
    role_dir.mkdir()
    common_dir.mkdir()
    role_agents = role_dir / "AGENTS.md"
    common_agents = common_dir / "AGENTS.md"
    role_agents.write_text("role guidance")
    common_agents.write_text("common guidance")
    (role_dir / "AGENT.md").write_text("legacy role guidance")
    (common_dir / "AGENT.md").write_text("legacy common guidance")

    role_mgr = object.__new__(RoleMgr)
    role_mgr._role_path = role_dir
    monkeypatch.setattr("src.mgr.role_mgr.common_role_dir", lambda: common_dir)

    assert role_mgr.agent_md_path() == role_agents
    assert role_mgr.common_agent_md_path() == common_agents

    role_agents.unlink()
    common_agents.unlink()
    assert role_mgr.agent_md_path() is None
    assert role_mgr.common_agent_md_path() is None


@pytest.mark.parametrize(
    ("is_subagent", "role_prompt"),
    [
        (False, "PRIMARY_IDENTITY"),
        (True, "SUBAGENT_IDENTITY"),
    ],
)
def test_agents_md_layers_are_shared_by_main_and_subagent(
    tmp_path: Path,
    is_subagent: bool,
    role_prompt: str,
) -> None:
    """主 agent 与子 agent 都获得四层 AGENTS.md，且不加载旧文件。"""
    common_dir = tmp_path / "common"
    role_dir = tmp_path / "role"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "project"
    for directory in (common_dir, role_dir, global_dir, workdir):
        directory.mkdir()

    expected_layers = [
        (common_dir, "COMMON_AGENTS_GUIDANCE"),
        (role_dir, "ROLE_AGENTS_GUIDANCE"),
        (global_dir, "GLOBAL_AGENTS_GUIDANCE"),
        (workdir, "PROJECT_AGENTS_GUIDANCE"),
    ]
    for directory, marker in expected_layers:
        (directory / "AGENTS.md").write_text(marker)
        (directory / "AGENT.md").write_text(f"LEGACY_{marker}")

    content = _build_prompt(
        is_subagent=is_subagent,
        role_mgr=_RoleMgrStub(
            common_dir / "AGENTS.md",
            role_dir / "AGENTS.md",
        ),
        workdir=workdir,
        global_dir=global_dir,
        role_prompt=role_prompt,
    )

    for _, marker in expected_layers:
        assert marker in content
        assert f"LEGACY_{marker}" not in content
    positions = [content.index(marker) for _, marker in expected_layers]
    assert positions == sorted(positions)
    assert role_prompt in content


def test_legacy_global_and_project_agents_files_are_ignored(
    tmp_path: Path,
) -> None:
    """全局层和项目层缺少 AGENTS.md 时也不能回退读取 AGENT.md。"""
    common_dir = tmp_path / "common"
    role_dir = tmp_path / "role"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "project"
    for directory in (common_dir, role_dir, global_dir, workdir):
        directory.mkdir()
    (common_dir / "AGENTS.md").write_text("COMMON_AGENTS_GUIDANCE")
    (role_dir / "AGENTS.md").write_text("ROLE_AGENTS_GUIDANCE")
    (global_dir / "AGENT.md").write_text("LEGACY_GLOBAL_ONLY")
    (workdir / "AGENT.md").write_text("LEGACY_PROJECT_ONLY")

    content = _build_prompt(
        is_subagent=False,
        role_mgr=_RoleMgrStub(
            common_dir / "AGENTS.md",
            role_dir / "AGENTS.md",
        ),
        workdir=workdir,
        global_dir=global_dir,
        role_prompt="PRIMARY_IDENTITY",
    )

    assert "COMMON_AGENTS_GUIDANCE" in content
    assert "ROLE_AGENTS_GUIDANCE" in content
    assert "LEGACY_GLOBAL_ONLY" not in content
    assert "LEGACY_PROJECT_ONLY" not in content


def test_coding_coordinator_guidance_only_reaches_main_prompt(
    tmp_path: Path,
) -> None:
    """编码角色的总控身份只进入主 agent，角色准则进入主/子提示词。"""
    role_dir = builtin_root() / "roles" / "coding"
    main_identity = _load_manifest_prompt(role_dir / "role.md", "main")
    child_identity = _load_manifest_prompt(role_dir / "agents" / "explore.md", "explore")
    global_dir = tmp_path / "global"
    workdir = tmp_path / "project"
    global_dir.mkdir()
    workdir.mkdir()
    role_mgr = _RoleMgrStub(None, role_dir / "AGENTS.md")

    main_content = _build_prompt(
        is_subagent=False,
        role_mgr=role_mgr,
        workdir=workdir,
        global_dir=global_dir,
        role_prompt=main_identity,
    )
    child_content = _build_prompt(
        is_subagent=True,
        role_mgr=role_mgr,
        workdir=workdir,
        global_dir=global_dir,
        role_prompt=child_identity,
    )

    assert "你是总控 agent" in main_content
    assert "你是总控 agent" not in child_content
    assert "# 编码角色共享行为准则" in main_content
    assert "# 编码角色共享行为准则" in child_content
