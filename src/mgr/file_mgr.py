from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from pathlib import Path

if TYPE_CHECKING:
    from src.agent import AgentDeps

@dataclass
class FileMgr:
    workdir: Path
    tool_results_dir: Path = field(init=False)
    deps: AgentDeps = field(repr=False)
    preview_chars: int = 2000
    persist_threshold: int = 30000

    def __post_init__(self):
        self.tool_results_dir = self.workdir / ".task_outputs" / "tool-results"

    def safe_path(self, path_str: str) -> Path:
        path = (self.workdir / path_str).resolve()
        if not path.is_relative_to(self.workdir):
            raise ValueError(f"Path escapes workspace: {path_str}")
        return path

    async def persist_large_output(self, tool_use_id: str, output: str) -> str:
        if len(output) <= self.persist_threshold:
            return output
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        stored_path = self.tool_results_dir / f"{tool_use_id}.txt"
        if not stored_path.exists():
            stored_path.write_text(output)
        preview = output[:self.preview_chars]
        rel_path = stored_path.relative_to(self.workdir)
        return (
            "<persisted-output>\n"
            f"Full output saved to: {rel_path}\n"
            "Preview:\n"
            f"{preview}\n"
            "</persisted-output>"
        )

    async def read_file(self, path: str, tool_use_id: str, limit: int | None = None) -> str:
        try:
            lines = self.safe_path(path).read_text().splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            output = "\n".join(lines)
            return await self.persist_large_output(tool_use_id, output)
        except Exception as exc:
            return f"Error: {exc}"

    async def write_file(self, path: str, content: str) -> str:
        try:
            file_path = self.safe_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as exc:
            return f"Error: {exc}"

    async def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        try:
            file_path = self.safe_path(path)
            content = file_path.read_text()
            if old_text not in content:
                return f"Error: Text not found in {path}"
            file_path.write_text(content.replace(old_text, new_text, 1))
            return f"Edited {path}"
        except Exception as exc:
            return f"Error: {exc}"
