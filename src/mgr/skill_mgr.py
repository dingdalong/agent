import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path

@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str

@dataclass
class SkillMgr:
    workdir: Path
    _documents: dict[str, SkillDocument] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        if not self.workdir.exists():
            return
        for path in sorted(self.workdir.rglob("SKILL.md")):
            meta, body = self._parse_frontmatter(path.read_text())
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "没有说明内容")
            manifest = SkillManifest(name=name, description=description, path=path)
            self._documents[name] = SkillDocument(manifest=manifest, body=body.strip())

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
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "\n".join(lines)

    def check_skill(self, name: str) -> bool:
        return name in self._documents

    def load_full_text(self, name: str) -> str:
        document = self._documents.get(name)
        if not document:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的技能：'{name}'。可用技能列表：{known}"
        parts = [
            f"<skill name=\"{document.manifest.name}\">",
            document.body,
        ]
        skill_dir = document.manifest.path.parent
        for path in sorted(
            p for p in skill_dir.iterdir()
            if p.is_file() and p.name != "SKILL.md"
        ):
            parts.append(f"\n<skill-file name=\"{path.name}\">")
            parts.append(path.read_text().strip())
            parts.append("</skill-file>")
        parts.append("</skill>")
        return "\n".join(parts)
