from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
}

_EXACT_MATCH_RE = re.compile(r'^[\w|]+$')


@dataclass(frozen=True)
class HookEntry:
    event: str
    matcher: str | None
    command: str
    timeout: float = 60.0
    async_: bool = False
    plugin_root: Path | None = None


@dataclass
class HookRunResult:
    additional_context: list[str] = field(default_factory=list)
    permission_decisions: list[tuple[str, str]] = field(default_factory=list)
    updated_input: dict[str, Any] | None = None
    blocked: bool = False
    block_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def merge(self, other: HookRunResult) -> None:
        self.additional_context.extend(other.additional_context)
        self.permission_decisions.extend(other.permission_decisions)
        if other.updated_input is not None:
            self.updated_input = other.updated_input
        if other.blocked:
            self.blocked = True
            self.block_reason = other.block_reason
        self.errors.extend(other.errors)


class HooksMgr:

    def __init__(self, workdir: str | Path, global_dir: Path | None = None):
        """初始化 hook 管理器 — 两层扫描：全局 → 项目，hooks 全部追加。

        Args:
            workdir: 用户工作目录（启动时 cwd），即项目根目录。
            global_dir: 全局配置目录（~/.agent/），为 None 时跳过全局层。
        """
        self.workdir = Path(workdir)
        self.global_dir = global_dir
        self.project_root = self.workdir
        self._hooks = self._load_hooks()

    def reload(self) -> None:
        """重新加载所有 hooks。"""
        self._hooks = self._load_hooks()

    # ── loading ──

    def _load_hooks(self) -> list[HookEntry]:
        """加载双层 hooks，全部追加。

        扫描顺序（全部追加，不覆盖）：
        全局 plugins → 全局 settings → 项目 plugins → 项目 settings

        Returns:
            所有加载到的 HookEntry 列表。
        """
        hooks: list[HookEntry] = []
        # 全局层
        if self.global_dir:
            global_plugins = self.global_dir / "plugins"
            if global_plugins.exists():
                for plugin_root in sorted(p for p in global_plugins.iterdir() if p.is_dir()):
                    hooks.extend(self._load_hook_file(
                        plugin_root / "hooks" / "hooks.json",
                        plugin_root=plugin_root,
                    ))
            hooks.extend(self._load_hook_file(self.global_dir / "settings.json"))
        # 项目层
        plugins_dir = self.workdir / ".agent" / "plugins"
        if plugins_dir.exists():
            for plugin_root in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
                hooks.extend(self._load_hook_file(
                    plugin_root / "hooks" / "hooks.json",
                    plugin_root=plugin_root,
                ))
        hooks.extend(self._load_hook_file(self.workdir / ".agent" / "settings.json"))
        return hooks

    def _load_hook_file(self, path: Path, *, plugin_root: Path | None = None) -> list[HookEntry]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("忽略 hook 文件 %s：JSON 格式无效：%s", path, exc)
            return []
        if not isinstance(raw, dict):
            return []

        hooks_config = raw.get("hooks", {})
        if not isinstance(hooks_config, dict):
            return []

        entries: list[HookEntry] = []
        for event, groups in hooks_config.items():
            if event not in HOOK_EVENTS:
                logger.warning("忽略未知 hook 事件 %s in %s", event, path)
                continue
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher")
                if matcher is not None and not isinstance(matcher, str):
                    continue
                for h in group.get("hooks", []):
                    if not isinstance(h, dict):
                        continue
                    if h.get("type", "command") != "command":
                        continue
                    command = h.get("command")
                    if not isinstance(command, str) or not command.strip():
                        continue
                    try:
                        timeout = max(0.1, float(h.get("timeout", 60)))
                    except (TypeError, ValueError):
                        timeout = 60.0
                    entries.append(HookEntry(
                        event=event,
                        matcher=matcher,
                        command=command,
                        timeout=timeout,
                        async_=bool(h.get("async", False)),
                        plugin_root=plugin_root,
                    ))
        return entries

    # ── execution ──

    async def run_event(
        self,
        event: str,
        match_value: str | None,
        extra: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        agent_id: str = "",
        agent_type: str = "",
        pre_tool: bool = False,
    ) -> HookRunResult:
        payload = {
            "hook_event_name": event,
            **(extra or {}),
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "cwd": str(self.project_root),
        }
        result = HookRunResult()
        for hook in self._matching_hooks(event, match_value):
            if hook.async_:
                self._schedule_async(hook, payload)
                continue
            hook_result = await self._run_hook(hook, payload, pre_tool=pre_tool)
            result.merge(hook_result)
            if hook_result.updated_input is not None:
                payload = {**payload, "tool_input": hook_result.updated_input}
        return result

    def _matching_hooks(self, event: str, value: str | None = None) -> list[HookEntry]:
        return [h for h in self._hooks if h.event == event and self._matches(h.matcher, value)]

    def _matches(self, matcher: str | None, value: str | None) -> bool:
        if not matcher or matcher == "*":
            return True
        if value is None:
            return False
        if _EXACT_MATCH_RE.match(matcher):
            return value in matcher.split("|")
        try:
            return re.fullmatch(matcher, value) is not None
        except re.error:
            return False

    def _schedule_async(self, hook: HookEntry, payload: dict[str, Any]) -> None:
        async def runner() -> None:
            try:
                await self._run_hook(hook, payload, pre_tool=False)
            except Exception:
                logger.exception("异步 hook 执行失败：%s", hook.command)
        try:
            asyncio.create_task(runner())
        except RuntimeError:
            logger.warning("无法调度异步 hook：%s", hook.command)

    async def _run_hook(
        self,
        hook: HookEntry,
        payload: dict[str, Any],
        *,
        pre_tool: bool,
    ) -> HookRunResult:
        env = os.environ.copy()
        if hook.plugin_root is not None:
            root = str(hook.plugin_root)
            env["CLAUDE_PLUGIN_ROOT"] = root
            env["AGENT_PLUGIN_ROOT"] = root

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_root),
                env=env,
            )
            stdin_data = json.dumps(payload, ensure_ascii=False).encode()
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=hook.timeout)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            msg = f"hook 超时：{hook.command}"
            logger.warning(msg)
            return self._error_result(msg, pre_tool)
        except OSError as exc:
            msg = f"hook 执行失败：{hook.command}: {exc}"
            logger.warning(msg)
            return self._error_result(msg, pre_tool)

        rc = proc.returncode
        if rc == 0:
            text = stdout.decode(errors="replace").strip()
            if not text:
                return HookRunResult()
            return self._parse_output(text)
        if rc == 2:
            err = stderr.decode(errors="replace").strip()
            return HookRunResult(blocked=True, block_reason=err or "hook blocked")
        # 其他非零：非阻止错误，记录但继续
        err = stderr.decode(errors="replace").strip()
        logger.warning("hook 返回状态 %d（非阻止）：%s", rc, err or hook.command)
        return HookRunResult()

    def _error_result(self, message: str, pre_tool: bool) -> HookRunResult:
        result = HookRunResult(errors=[message])
        if pre_tool:
            result.permission_decisions.append(("ask", message))
        return result

    # ── output parsing ──

    def _parse_output(self, text: str) -> HookRunResult:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return HookRunResult(additional_context=[text])
        if not isinstance(raw, dict):
            return HookRunResult(additional_context=[text])

        result = HookRunResult()
        self._extract_fields(raw, result)
        if raw.get("decision") == "block":
            result.blocked = True
            reason = raw.get("reason")
            result.block_reason = str(reason) if reason is not None else "hook blocked"

        specific = raw.get("hookSpecificOutput")
        if isinstance(specific, dict):
            self._extract_fields(specific, result)
        return result

    def _extract_fields(self, raw: dict[str, Any], result: HookRunResult) -> None:
        ctx = raw.get("additionalContext")
        if isinstance(ctx, str) and ctx:
            result.additional_context.append(ctx)
        elif isinstance(ctx, list):
            result.additional_context.extend(str(item) for item in ctx if item)

        decision = raw.get("permissionDecision")
        if isinstance(decision, str):
            normalized = decision.strip().lower()
            if normalized in {"allow", "deny", "ask", "defer"}:
                reason = raw.get("permissionDecisionReason") or raw.get("reason")
                result.permission_decisions.append((normalized, str(reason or f"hook {normalized}")))

        updated = raw.get("updatedInput")
        if isinstance(updated, dict):
            result.updated_input = updated
