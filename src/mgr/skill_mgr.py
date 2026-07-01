from __future__ import annotations

import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.mgr.plugin_mgr import PluginMgr
    from src.mgr.role_mgr import RoleMgr

@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path

@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str
    full_text: str

@dataclass
class SkillMgr:
    """技能管理器 — 四层扫描：共享 → 角色 → 全局 → 项目。

    Args:
        workdir: 用户工作目录。
        global_dir: 全局配置目录（~/.agent/）。
        plugin_mgr: 插件管理器，为 None 时跳过插件层技能。
        role_mgr: 角色管理器，为 None 时跳过角色层技能。
    """
    workdir: Path
    global_dir: Path | None = None
    plugin_mgr: PluginMgr | None = None
    role_mgr: RoleMgr | None = None
    _documents: dict[str, SkillDocument] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._load_all()

    def _load_all(self) -> None:
        """扫描四层目录加载所有技能，同名技能后者覆盖前者。

        扫描顺序（低→高优先级）：
        共享 skills → 角色 skills → 全局 plugins → 全局 skills → 项目 plugins → 项目 skills
        """
        from src.mgr.plugin_mgr import PluginLayer

        project_skills = self.workdir / ".agent" / "skills"

        # (目录, 命名空间) — 按优先级从低到高排列
        scan_dirs: list[tuple[Path, str]] = []

        # 共享技能（最低优先级，所有角色可用）
        if self.role_mgr is not None:
            cd = self.role_mgr.common_skills_dir()
            if cd is not None:
                scan_dirs.append((cd, "common"))

        # 角色技能（基准层）
        if self.role_mgr is not None and self.role_mgr.active:
            sd = self.role_mgr.skills_dir()
            if sd is not None:
                scan_dirs.append((sd, self.role_mgr.role_name or "role"))

        if self.global_dir:
            # 全局插件技能
            if self.plugin_mgr is not None:
                for p in self.plugin_mgr.plugins(layer=PluginLayer.GLOBAL):
                    scan_dirs.append((p.root, p.name))
            # 全局用户技能
            scan_dirs.append((self.global_dir / "skills", "user"))

        # 项目插件技能
        if self.plugin_mgr is not None:
            for p in self.plugin_mgr.plugins(layer=PluginLayer.PROJECT):
                scan_dirs.append((p.root, p.name))
        # 项目用户技能
        scan_dirs.append((project_skills, "user"))

        for src_dir, namespace in scan_dirs:
            if not src_dir.exists():
                continue
            for path in sorted(src_dir.rglob("SKILL.md")):
                self._load_skill(path, namespace)

    def _load_skill(self, path: Path, namespace: str) -> None:
        """解析并注册单个 SKILL.md 文件，同名覆盖。

        Args:
            path: SKILL.md 文件路径。
            namespace: 命名空间前缀（如 builtin、user、插件名）。
        """
        meta, body = self._parse_frontmatter(path.read_text())
        skill_name = meta.get("name", path.parent.name)
        name = f"{namespace}:{skill_name}"
        description = meta.get("description", "没有说明内容")
        manifest = SkillManifest(name=name, description=description, path=path)
        skill_dir = manifest.path.parent.resolve()
        try:
            skill_dir_rel = skill_dir.relative_to(self.workdir)
        except ValueError:
            skill_dir_rel = skill_dir
        parts = [
            f"<skill name=\"{manifest.name}\" skill_dir=\"{skill_dir_rel}\">",
            body.strip(),
        ]
        for skill_file in sorted(
            p for p in skill_dir.iterdir()
            if p.is_file() and p.name != "SKILL.md"
        ):
            rel_path = skill_file.relative_to(skill_dir).as_posix()
            parts.append(f"<skill-file path=\"{rel_path}\" ref=\"{skill_dir_rel}/{rel_path}\" />")
        parts.append("</skill>")
        self._documents[name] = SkillDocument(
            manifest=manifest,
            body=body.strip(),
            full_text="\n".join(parts),
        )

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = yaml.safe_load(match.group(1)) or {}
        return meta, match.group(2)

    def describe(self) -> str | None:
        if not self._documents:
            return
        lines = []
        for name in sorted(self._documents):
            manifest = self._documents[name].manifest
            lines.append(f"- [{manifest.name}]: {manifest.description}")
        return "\n".join(lines)

    def prompt_section(self) -> str:
        """返回技能列表提示词段（含使用流程），无技能时返回空串。"""
        describe = self.describe()
        if not describe:
            return ""
        return (
            "# 可用技能\n" + describe +
            "\n\n## 技能使用流程\n"
            "当任务匹配某个技能时，调用 load_skill 加载后再执行操作。"
            "已加载技能的指令优先于本文的通用规则。"
        )

    def check_skill(self, name: str) -> bool:
        return name in self._documents

    def load_full_text(self, name: str) -> str:
        document = self._documents.get(name)
        if not document:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的技能：'{name}'。可用技能列表：{known}"
        return document.full_text
