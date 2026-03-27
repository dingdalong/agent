"""SkillManager: 发现、披露、激活 Agent Skills。"""

import html
import logging
from pathlib import Path
from typing import Optional

from .models import SkillInfo
from .parser import find_skill_md, parse_frontmatter, read_skill_info

logger = logging.getLogger(__name__)

_RESERVED_COMMANDS = {"book"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_MAX_SCAN_DIRS = 2000


class SkillManager:
    def __init__(self, skill_dirs: list[str]):
        self._skill_dirs = skill_dirs
        self._skills: dict[str, SkillInfo] = {}
        self._activated: set[str] = set()

    async def discover(self) -> None:
        self._skills.clear()
        self._activated.clear()
        dirs_scanned = 0
        for skill_dir_path in self._skill_dirs:
            base = Path(skill_dir_path)
            if not base.exists() or not base.is_dir():
                logger.debug(f"Skill 目录不存在: {base}")
                continue
            for sub in sorted(base.iterdir()):
                if dirs_scanned >= _MAX_SCAN_DIRS:
                    logger.warning(f"已达到最大扫描目录数 {_MAX_SCAN_DIRS}，停止扫描")
                    break
                if not sub.is_dir():
                    continue
                if sub.name in _SKIP_DIRS or sub.name.startswith("."):
                    continue
                dirs_scanned += 1
                try:
                    info = read_skill_info(sub)
                except (FileNotFoundError, ValueError) as e:
                    logger.debug(f"跳过 {sub.name}: {e}")
                    continue
                if info.name in self._skills:
                    logger.warning(f"Skill 名称冲突: '{info.name}' 已存在，跳过 {sub}")
                    continue
                self._skills[info.name] = info
                logger.info(f"发现 Skill: {info.name}")

    def get_skill(self, name: str) -> Optional[SkillInfo]:
        return self._skills.get(name)

    def get_catalog_prompt(self) -> str:
        if not self._skills:
            return ""
        lines = [
            "以下 skills 提供了特定任务的专业指令。",
            "当任务匹配某个 skill 的描述时，调用 activate_skill 工具加载其完整指令。",
            "",
            "<available_skills>",
        ]
        for info in self._skills.values():
            lines.append("<skill>")
            lines.append("<name>")
            lines.append(html.escape(info.name))
            lines.append("</name>")
            lines.append("<description>")
            lines.append(html.escape(info.description))
            lines.append("</description>")
            lines.append("<location>")
            lines.append(str(info.location))
            lines.append("</location>")
            lines.append("</skill>")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def activate(self, skill_name: str) -> Optional[str]:
        info = self._skills.get(skill_name)
        if info is None:
            return None
        if info.body is None:
            content = info.location.read_text(encoding="utf-8")
            _, body = parse_frontmatter(content)
            info.body = body
        self._activated.add(skill_name)
        skill_dir = info.location.parent
        resources = self.list_resources(skill_name)
        parts = [f'<skill_content name="{html.escape(skill_name)}">']
        parts.append(info.body)
        parts.append("")
        parts.append(f"Skill 目录: {skill_dir}")
        parts.append("此 skill 中的相对路径基于上述目录。")
        if resources:
            parts.append("")
            parts.append("<skill_resources>")
            for res in resources:
                parts.append(f"  <file>{res}</file>")
            parts.append("</skill_resources>")
        parts.append("</skill_content>")
        return "\n".join(parts)

    def list_resources(self, skill_name: str) -> list[str]:
        info = self._skills.get(skill_name)
        if info is None:
            return []
        skill_dir = info.location.parent
        resources = []
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name in ("SKILL.md", "skill.md"):
                continue
            rel = path.relative_to(skill_dir)
            resources.append(str(rel))
        return resources

    def build_activate_tool_schema(self) -> Optional[dict]:
        if not self._skills:
            return None
        names = sorted(self._skills.keys())
        return {
            "type": "function",
            "function": {
                "name": "activate_skill",
                "description": "激活指定的 Skill，加载其完整指令到当前对话中。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": names,
                            "description": "要激活的 Skill 名称",
                        },
                    },
                    "required": ["name"],
                },
            },
        }

    def is_slash_command(self, user_input: str) -> Optional[str]:
        if not user_input.startswith("/"):
            return None
        parts = user_input[1:].split(None, 1)
        if not parts:
            return None
        cmd = parts[0]
        if cmd in _RESERVED_COMMANDS:
            return None
        if cmd in self._skills:
            return cmd
        return None
