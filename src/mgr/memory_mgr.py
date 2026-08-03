from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from src.mgr.secure_io import atomic_write_text

logger = logging.getLogger(__name__)

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES = get_args(MemoryType)


@dataclass
class MemoryEntry:
    title: str
    description: str
    type: MemoryType
    update_at: str
    body: str
    path: Path


@dataclass
class MemoryMgr:
    workdir: Path
    max_prompt_entries: int = 50
    data_guard: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir)
        self.memory_dir = self.workdir / ".agent" / "memory"
        self.entries: dict[str, MemoryEntry] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.memory_dir.exists():
            self.entries = {}
            return

        entries: dict[str, MemoryEntry] = {}
        for path in sorted(self.memory_dir.glob("*.md")):
            entry = self._load_entry(path)
            if entry is None:
                continue
            entries[entry.title] = entry

        self.entries = self._sort_entries(entries)

    def reload(self) -> None:
        self._load_all()

    def build_prompt(self) -> str:
        """构建项目记忆提示词段。无记忆时返回空字符串。"""
        entries = list(self.entries.values())
        selected = entries[: self.max_prompt_entries]

        if not selected:
            return ""

        parts = [
            "# 项目记忆",
            "使用记忆的方式：",
            "1. 先把 memory 当作方向提示。",
            "2. 再去读当前文件、当前资源、当前配置。",
            "3. 如果冲突，优先相信你刚观察到的真实状态。",
            "保存记忆前，先检查已知记忆简报。",
            "如果新信息和已有记忆语义相近，属于同一主题、同一偏好、同一约束或同一类反馈，"
            "不要创建新标题；复用已有记忆的原标题，必要时用 read_memory 读取旧正文，"
            "合并新旧内容后调用 save_memory 用同一标题全量覆盖。",
            "只有主题、适用范围或用途明显不同，才创建新的记忆标题。",
            "## 已知记忆（简报）：",
        ]

        for memory_type in MEMORY_TYPES:
            group = [entry for entry in selected if entry.type == memory_type]
            if not group:
                continue
            parts.append(f"## {memory_type}")
            for entry in group:
                description = entry.description or "(无描述)"
                parts.append(
                    "\n".join(
                        [
                            f"- 标题: {entry.title}",
                            f"- 描述: {description}",
                            f"- 更新时间: {entry.update_at}",
                        ]
                    )
                )

        rendered = "\n".join(parts)
        rendered += "\n当需要了解某个记忆的详细内容时，使用 read_memory 加载。"
        return rendered

    def save(
        self,
        title: str,
        description: str,
        type: str,
        body: str,
    ) -> str:
        if self.data_guard is not None:
            title, description, body = (
                str(self.data_guard.redact(item)) for item in (title, description, body)
            )
        title = title.strip()
        validation_error = self._validate_title(title)
        if validation_error:
            return validation_error
        type_error = self._validate_type(type)
        if type_error:
            return type_error

        path = self._path_for_title(title)

        entry = MemoryEntry(
            title=title,
            description=description.strip(),
            type=type,  # type: ignore[arg-type]
            update_at=self._now(),
            body=body.strip(),
            path=path,
        )
        self._write_entry(entry)
        self.entries[entry.title] = entry
        self.entries = self._sort_entries(self.entries)
        return title

    def read(self, title: str) -> str:
        validation_error = self._validate_title(title)
        if validation_error:
            return validation_error
        entry = self.entries.get(title.strip())
        if entry is None:
            return f"错误：不存在的项目记忆：{title}"
        return "\n".join(
            [
                f"# {entry.title}",
                f"- type: {entry.type}",
                f"- description: {entry.description}",
                f"- update_at: {entry.update_at}",
                "",
                entry.body,
            ]
        ).rstrip()

    def _load_entry(self, path: Path) -> MemoryEntry | None:
        try:
            parsed = self._parse_frontmatter(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("跳过项目记忆文件 %s：读取或解析失败：%s", path, exc)
            return None
        if parsed is None:
            logger.warning("跳过项目记忆文件 %s：缺少 frontmatter", path)
            return None
        meta, body = parsed
        required = ("title", "description", "type", "update_at")
        if any(meta.get(key) is None for key in required):
            logger.warning("跳过项目记忆文件 %s：缺少必要字段", path)
            return None

        title = str(meta["title"]).strip()
        if self._validate_title(title):
            logger.warning("跳过项目记忆文件 %s：title 为空", path)
            return None
        memory_type = str(meta["type"]).strip()
        if self._validate_type(memory_type):
            logger.warning("跳过项目记忆文件 %s：type 无效：%s", path, memory_type)
            return None
        entry = MemoryEntry(
            title=title,
            description=str(meta["description"] or "").strip(),
            type=memory_type,  # type: ignore[arg-type]
            update_at=str(meta["update_at"]).strip(),
            body=body.strip(),
            path=path,
        )
        if self.data_guard is not None:
            entry.title = str(self.data_guard.redact(entry.title))
            entry.description = str(self.data_guard.redact(entry.description))
            entry.body = str(self.data_guard.redact(entry.body))
        return entry

    def _parse_frontmatter(self, text: str) -> tuple[dict, str] | None:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
        if not match:
            return None
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            return None
        return meta, match.group(2)

    def _write_entry(self, entry: MemoryEntry) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "title": entry.title,
            "description": entry.description,
            "type": entry.type,
            "update_at": entry.update_at,
        }
        frontmatter = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
        atomic_write_text(
            entry.path,
            f"---\n{frontmatter}\n---\n\n{entry.body.rstrip()}\n",
        )

    def _sort_entries(self, entries: dict[str, MemoryEntry]) -> dict[str, MemoryEntry]:
        return dict(
            sorted(
                entries.items(),
                key=lambda item: (item[1].update_at, item[0].lower()),
                reverse=True,
            )
        )

    def _path_for_title(self, title: str) -> Path:
        return self.memory_dir / f"{self._slugify(title)}.md"

    def _validate_title(self, title: str | None) -> str | None:
        if title is None or not str(title).strip():
            return "错误：title 不能为空。"
        return None

    def _validate_type(self, type: str) -> str | None:
        if type not in MEMORY_TYPES:
            valid = ", ".join(MEMORY_TYPES)
            return f"错误：type 必须是以下之一：{valid}。"
        return None

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _slugify(self, title: str) -> str:
        slug = re.sub(r"[^\w-]+", "-", title.strip().lower(), flags=re.UNICODE)
        slug = re.sub(r"-+", "-", slug).strip("-_")
        return slug or "memory"
