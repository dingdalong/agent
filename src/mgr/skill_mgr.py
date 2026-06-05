import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from src.mgr.paths import builtin_root

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
    """技能管理器 — 三层扫描：内置 → 全局 → 项目。

    Args:
        workdir: 用户工作目录。
        global_dir: 全局配置目录（~/.agent/）。
    """
    workdir: Path
    global_dir: Path | None = None
    _documents: dict[str, SkillDocument] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        """扫描三层目录加载所有技能，同名技能后者覆盖前者。

        扫描顺序（低→高优先级）：
        内置 skills → 全局 plugins → 全局 skills → 项目 plugins → 项目 skills
        """
        builtin_dir = builtin_root() / "skills"
        project_plugins = self.workdir / ".agent" / "plugins"
        project_skills = self.workdir / ".agent" / "skills"

        # (目录, 是否为 plugins 目录) — plugins 目录用插件名做命名空间，其他用固定名
        scan_dirs: list[tuple[Path, bool]] = [(builtin_dir, False)]
        if self.global_dir:
            scan_dirs.append((self.global_dir / "plugins", True))
            scan_dirs.append((self.global_dir / "skills", False))
        scan_dirs.append((project_plugins, True))
        scan_dirs.append((project_skills, False))

        for src_dir, is_plugins in scan_dirs:
            if not src_dir.exists():
                continue
            for path in sorted(src_dir.rglob("SKILL.md")):
                namespace = path.relative_to(src_dir).parts[0] if is_plugins else "builtin" if src_dir == builtin_dir else "user"
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

    def check_skill(self, name: str) -> bool:
        return name in self._documents

    def load_full_text(self, name: str) -> str:
        document = self._documents.get(name)
        if not document:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的技能：'{name}'。可用技能列表：{known}"
        return document.full_text
