"""PromptMgr 分层加载项目行为准则的回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.mgr.prompt_mgr import PromptMgr


def test_project_agents_md_is_loaded(tmp_path: Path) -> None:
    """项目 AGENTS.md 应加载为项目层行为准则。

    Args:
        tmp_path: pytest 提供的临时项目目录。
    """
    (tmp_path / "AGENTS.md").write_text("project guidance")
    agent = SimpleNamespace(deps=SimpleNamespace(role_mgr=None))
    prompt_mgr = PromptMgr(agent=agent, model="test-model", workdir=tmp_path)

    guidance = prompt_mgr._build_agent_md()

    assert "project guidance" in guidance
