from __future__ import annotations
from typing import TYPE_CHECKING

import re
from dataclasses import dataclass, field
from pathlib import Path

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps

@dataclass
class SubAgentManifest:
    name: str
    description: str
    path: Path
    tools: set[str]

@dataclass
class SubAgentDocument:
    manifest: SubAgentManifest
    prompt: str

@dataclass
class SubAgentMgr:
    workdir: Path
    deps: AgentDeps = field(repr=False)

    _documents: dict[str, SubAgentDocument] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        builtin_dir = Path(__file__).resolve().parent.parent / "agent" / "agents"
        workspace_dir = self.workdir / ".agents"

        for directory in (builtin_dir, workspace_dir):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                meta, prompt = self._parse_frontmatter(path.read_text())
                name = meta.get("name", path.stem)
                description = meta.get("description", "没有说明内容")
                raw_tools = meta.get("tools", "")
                tools = {t.strip() for t in raw_tools.split(",") if t.strip()}
                if tools:
                    tools.add("read_tool_result")
                manifest = SubAgentManifest(
                    name=name,
                    description=description,
                    path=path,
                    tools=tools,
                )
                self._documents[name] = SubAgentDocument(manifest=manifest, prompt=prompt.strip())

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        return meta, match.group(2)

    def describe(self) -> str | None:
        if not self._documents:
            return
        lines = []
        for name in sorted(self._documents):
            manifest = self._documents[name].manifest
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "\n".join(lines)

    async def task_delegator(self, name: str, prompt: str) -> str:

        document = self._documents.get(name)
        if not document:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的子智能体：'{name}'。可用子智能体列表：{known}"

        from src.agent import Agent
        agent = Agent(
            name = name,
            description = document.manifest.description,
            deps = self.deps,
            tools = document.manifest.tools,
            is_subagent = True,
        )
        history = []
        return await agent.run(prompt, history)
