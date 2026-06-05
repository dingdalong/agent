import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from itertools import chain

BUILTIN_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills"

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
    workdir: Path
    _documents: dict[str, SkillDocument] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        builtin_dir = BUILTIN_SKILL_DIR
        plugins_dir = self.workdir / ".agent" / "plugins"
        workspace_dir = self.workdir / ".agent" / "skills"

        if not self.workdir.exists():
            return

        paths = chain(
            ((path, "builtin") for path in sorted(builtin_dir.rglob("SKILL.md"))),
            (
                (path, path.relative_to(plugins_dir).parts[0])
                for path in sorted(plugins_dir.rglob("SKILL.md"))
            ),
            ((path, "user") for path in sorted(workspace_dir.rglob("SKILL.md"))),
        )

        for path, namespace in paths:
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
