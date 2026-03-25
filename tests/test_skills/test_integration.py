"""端到端集成测试：Skill 发现 → 激活 → 工具路由。"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path
from src.skills import SkillManager
from src.tools.tool_executor import ToolExecutor


def _make_skill(base, name, description="A skill.", body="## Do things"):
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    )
    return skill_dir


@pytest.mark.asyncio
async def test_discover_activate_end_to_end(tmp_path):
    """Full cycle: discover → catalog → activate → verify content."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_skill(skills_dir, "test-skill", "A test skill.", "## Steps\n1. Do it")

    mgr = SkillManager(skill_dirs=[str(skills_dir)])
    await mgr.discover()

    # Catalog contains the skill
    catalog = mgr.get_catalog_prompt()
    assert "test-skill" in catalog

    # Activate returns wrapped content
    result = mgr.activate("test-skill")
    assert '<skill_content name="test-skill">' in result
    assert "## Steps" in result
    assert "1. Do it" in result


@pytest.mark.asyncio
async def test_tool_executor_skill_routing(tmp_path):
    """ToolExecutor correctly routes activate_skill to SkillManager."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_skill(skills_dir, "my-skill", "My skill.", "## Body")

    mgr = SkillManager(skill_dirs=[str(skills_dir)])
    await mgr.discover()

    executor = ToolExecutor({})
    executor.skill_manager = mgr

    result = await executor.execute("activate_skill", {"name": "my-skill"})
    assert "skill_content" in result
    assert "## Body" in result


@pytest.mark.asyncio
async def test_slash_command_full_flow(tmp_path):
    """Slash command detection + activation flow."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_skill(skills_dir, "translate", "Translate text.", "## Translate\n1. Do it")

    mgr = SkillManager(skill_dirs=[str(skills_dir)])
    await mgr.discover()

    # Slash command detected
    skill_name = mgr.is_slash_command("/translate hello world")
    assert skill_name == "translate"

    # Activate returns content
    result = mgr.activate(skill_name)
    assert "## Translate" in result


@pytest.mark.asyncio
async def test_activate_tool_schema_used_by_executor(tmp_path):
    """activate_skill schema has correct enum matching discovered skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_skill(skills_dir, "a-skill", "Skill A.")
    _make_skill(skills_dir, "b-skill", "Skill B.")

    mgr = SkillManager(skill_dirs=[str(skills_dir)])
    await mgr.discover()

    schema = mgr.build_activate_tool_schema()
    enum = schema["function"]["parameters"]["properties"]["name"]["enum"]
    assert "a-skill" in enum
    assert "b-skill" in enum
