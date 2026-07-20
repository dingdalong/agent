"""SkillMgr 的项目嵌套技能发现与按需加载测试。"""

from __future__ import annotations

from pathlib import Path

from src.mgr.skill_mgr import SkillMgr


def test_project_nested_skill_exposes_metadata_before_loading_body(tmp_path: Path) -> None:
    """项目嵌套技能应在提示词中暴露元数据，并仅在显式加载时返回正文。"""
    skill_path = (
        tmp_path
        / ".agent"
        / "skills"
        / "onboard"
        / "add-client-proto"
        / "SKILL.md"
    )
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: onboard-add-client-proto
description: 新增客户端协议时加载。
---

# 仅按需加载的实施步骤
"""
    )

    skill_mgr = SkillMgr(workdir=tmp_path)

    prompt = skill_mgr.prompt_section()
    assert prompt is not None
    assert "user:onboard-add-client-proto" in prompt
    assert "新增客户端协议时加载。" in prompt
    assert "仅按需加载的实施步骤" not in prompt

    loaded = skill_mgr.load_full_text("user:onboard-add-client-proto")
    assert "仅按需加载的实施步骤" in loaded
    assert 'skill_dir=".agent/skills/onboard/add-client-proto"' in loaded
