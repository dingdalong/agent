"""PromptMgr 分层加载项目行为准则的回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.mgr.prompt_mgr import PromptFacts, PromptMgr


def test_project_agents_md_is_external_context(tmp_path: Path) -> None:
    """项目 AGENTS.md 只作为带来源的外部上下文加载。

    Args:
        tmp_path: pytest 提供的临时项目目录。
    """
    (tmp_path / "AGENTS.md").write_text("project guidance")
    prompt_mgr = PromptMgr(
        facts=PromptFacts(),
        deps=SimpleNamespace(role_mgr=None, memory_mgr=None, session_context=[]),
        workdir=tmp_path,
    )

    system = prompt_mgr.build()[0]["content"]
    context = prompt_mgr.build_initial_context_messages()[0]

    assert "project guidance" not in system
    assert context["role"] == "user"
    assert '<external_context source="AGENTS.md" layer="project">' in context["content"]
    assert "project guidance" in context["content"]
