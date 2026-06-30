from __future__ import annotations
from typing import Any, TYPE_CHECKING

import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.events.types import SubagentLifecycle
from src.mgr.role_mgr import parse_frontmatter, extract_manifest, AgentManifest

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps

logger = logging.getLogger(__name__)


@dataclass
class SubAgentMgr:
    """子智能体管理器 — 四层扫描：共享 → 角色 → 全局 → 项目，同名覆盖。

    Args:
        workdir: 用户工作目录。
        deps: 外部依赖。
        global_dir: 全局配置目录（~/.agent/）。
    """
    workdir: Path
    deps: AgentDeps = field(repr=False)
    global_dir: Path | None = None

    _documents: dict[str, AgentManifest] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        """扫描四层目录加载子智能体定义，同名后者覆盖前者。

        扫描顺序（低→高优先级）：共享 → 角色 → 全局 → 项目。
        """
        project_dir = self.workdir / ".agent" / "agents"

        scan_dirs: list[tuple[Path, str]] = []

        # 共享子 agent（最低优先级，所有角色可用）
        role_mgr = getattr(self.deps, "role_mgr", None)
        if role_mgr is not None:
            cd = role_mgr.common_agents_dir()
            if cd is not None:
                scan_dirs.append((cd, "common"))

        # 角色子 agent（基准层）
        if role_mgr is not None and role_mgr.active:
            sd = role_mgr.agents_dir()
            if sd is not None:
                scan_dirs.append((sd, "role"))
        if self.global_dir:
            scan_dirs.append((self.global_dir / "agents", "global"))
        scan_dirs.append((project_dir, "project"))

        for directory, _source in scan_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                meta, prompt = parse_frontmatter(path.read_text())
                manifest = extract_manifest(meta, path, prompt=prompt)
                self._documents[manifest.agent_type] = manifest

    def describe(self) -> str | None:
        if not self._documents:
            return
        lines = []
        for manifest in self._documents.values():
            lines.append(f"- {manifest.agent_type}: {manifest.description}")
        return "\n".join(lines)

    async def task_delegator(
        self,
        agent_type: str,
        prompt: str,
        *,
        parent_agent: Any = None,
        task_id: str | None = None,
    ) -> str:
        """委派任务给子智能体并返回执行结果。

        若指定 task_id，委派前自动将任务标记为 in_progress 并设置 owner；
        子智能体异常退出时自动回滚为 pending。正常返回时不标 completed，
        留给主 agent 评估结果后决定。

        Args:
            agent_type: 目标子智能体类型标识。
            prompt: 传给子智能体的完整任务正文。
            parent_agent: 调用方 Agent 实例，用于管理父任务状态和触发 hooks。
            task_id: 关联的任务 ID（可选），指定后框架自动管理任务状态。

        Returns:
            子智能体的执行结果文本，或错误信息。
        """
        manifest = self._documents.get(agent_type)
        if not manifest:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的子智能体：'{agent_type}'。可用子智能体列表：{known}"

        # —— 自动标记任务为 in_progress ——
        task_mgr = getattr(parent_agent, '_task_mgr', None) if parent_agent else None
        if task_id and task_mgr:
            try:
                task_mgr.update(task_id, status="in_progress", owner=agent_type)
            except ValueError:
                pass

        # 解析子 agent 的最终工具集（自动注入 subagent=True、排除 subagent=False）
        tools = self.deps.tools_mgr.resolve_subagent_tools(manifest.tools)

        # 解析模型：inherit 表示继承父 agent 已解析的真实模型 ID
        model_value = manifest.model
        if model_value == "inherit" and parent_agent is not None:
            model_value = parent_agent.llm.model

        # 思考模式：显式设置则用设置值，否则继承父 agent
        enable_thinking = manifest.enable_thinking
        if enable_thinking is None:
            enable_thinking = getattr(parent_agent, "enable_thinking", True)

        from src.agent import Agent
        agent = Agent.from_manifest(
            manifest=manifest,
            deps=self.deps,
            is_subagent=True,
            tools=tools,
            model=model_value,
            enable_thinking=enable_thinking,
        )

        hooks_mgr = self.deps.hooks_mgr
        fire_hooks = hooks_mgr is not None and parent_agent is not None
        hook_kwargs = {}
        if fire_hooks:
            hook_kwargs = {
                "session_id": self.deps.session_id,
                "agent_id": str(getattr(parent_agent, "uuid", "")),
                "agent_type": getattr(parent_agent, "agent_type", ""),
            }
            await hooks_mgr.run_event(
                "SubagentStart",
                agent_type,
                {"subagent_type": agent_type, "subagent_id": str(agent.uuid), "prompt": prompt},
                **hook_kwargs,
            )

        # 获取 event_bus（用于发射 SubagentLifecycle），异常/取消时 finally 也需引用
        event_bus = getattr(self.deps, "event_bus", None)

        try:
            # 发射子 agent 生命周期开始事件
            if event_bus is not None:
                await event_bus.emit(SubagentLifecycle(
                    timestamp=time.time(),
                    source="subagent_mgr",
                    agent_uuid=str(agent.uuid),
                    agent_type=agent_type,
                    phase="start",
                ))

            result = (await agent.run(prompt)).final_text
        except Exception:
            # —— 子智能体异常退出，回滚任务状态 ——
            if task_id and task_mgr:
                try:
                    task_mgr.update(task_id, status="pending", owner="")
                except ValueError:
                    pass
            raise
        finally:
            # 发射子 agent 生命周期结束事件（异常/取消也发）
            if event_bus is not None:
                await event_bus.emit(SubagentLifecycle(
                    timestamp=time.time(),
                    source="subagent_mgr",
                    agent_uuid=str(agent.uuid),
                    agent_type=agent_type,
                    phase="end",
                ))

        if fire_hooks:
            stop_result = await hooks_mgr.run_event(
                "SubagentStop",
                agent_type,
                {"subagent_type": agent_type, "subagent_id": str(agent.uuid), "result": result},
                **hook_kwargs,
            )
            if stop_result.blocked:
                result = stop_result.block_reason or result
            elif stop_result.additional_context:
                result = result + "\n\n" + "\n\n".join(str(c) for c in stop_result.additional_context)

        return result
