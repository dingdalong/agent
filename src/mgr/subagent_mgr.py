from __future__ import annotations
from typing import TYPE_CHECKING

import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps

@dataclass
class SubAgentManifest:
    agent_type: str
    description: str
    path: Path
    tools: set[str] | None = None
    memory: str | None = None

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
                agent_type = meta.get("agent_type", path.stem)
                description = meta.get("description", "没有说明内容")
                raw_tools = meta.get("tools", "")
                tools = {t.strip() for t in raw_tools.split(",") if t.strip()} or None
                if tools:
                    tools.add("read_tool_result")
                memory = meta.get("memory")
                manifest = SubAgentManifest(
                    agent_type=agent_type,
                    description=description,
                    path=path,
                    tools=tools,
                    memory=memory,
                )
                self._documents[agent_type] = SubAgentDocument(manifest=manifest, prompt=prompt.strip())

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
        for agent_type in sorted(self._documents):
            manifest = self._documents[agent_type].manifest
            lines.append(f"- {manifest.agent_type}: {manifest.description}")
        return "\n".join(lines)

    async def task_delegator(self, agent_type: str, prompt: str) -> str:
        document = self._documents.get(agent_type)
        if not document:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的子智能体：'{agent_type}'。可用子智能体列表：{known}"

        from src.agent import Agent
        agent = Agent(
            agent_type = agent_type,
            description = document.manifest.description,
            role_prompt = document.prompt or None,
            deps = self.deps,
            tools = document.manifest.tools,
            is_subagent = True,
            memory = document.manifest.memory,
        )
        history = []
        return await agent.run(prompt, history)
