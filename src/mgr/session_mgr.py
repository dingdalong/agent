"""会话管理器 — 管理会话元数据和对话历史的持久化与恢复。

会话数据存储在 {global_dir}/sessions/ 下：
- {session_id}.json     — 元数据（workdir、时间戳、主题）
- {session_id}.hist.json — 对话历史快照（每次完整覆写，正确处理 compact）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ResumeResult:
    """resolve_resume() 解析成功时的返回值。

    Attributes:
        session_id: 目标会话 UUID。
        messages: 加载的完整对话历史。
        metadata: 目标会话的元数据字典（含 permission_mode、pre_plan_mode、topic 等）。
    """
    session_id: str
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionMgr:
    """会话管理器 — 持久化会话元数据和对话历史，支持 /resume 恢复。

    Attributes:
        _sessions_dir: 会话文件存储目录（{global_dir}/sessions/）。
        _workdir: 当前工作目录，记录到元数据中。
    """

    def __init__(self, global_dir: Path, workdir: Path) -> None:
        """初始化会话管理器。

        Args:
            global_dir: 全局配置目录（~/.agent/）。
            workdir: 当前工作目录。
        """
        self._sessions_dir: Path = global_dir / "sessions"
        self._workdir: Path = workdir

    def save_metadata(
        self,
        session_id: str,
        *,
        is_new: bool = False,
        topic: str = "",
        permission_mode: str = "",
        pre_plan_mode: str = "",
    ) -> None:
        """保存或更新会话元数据文件。

        首次调用（is_new=True）写入 created_at；后续调用仅更新 updated_at。

        Args:
            session_id: 会话 UUID。
            is_new: 是否为新会话首次写入。
            topic: 会话主题（截取前 100 字符）。非空时写入或覆盖 topic 字段。
            permission_mode: 当前权限模式的 value 字符串（如 "default", "plan"）。
            pre_plan_mode: plan 模式下进入前的权限模式 value 字符串，非 plan 模式时传空。
        """
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self._sessions_dir / f"{session_id}.json"
        now = datetime.now(timezone.utc).isoformat()

        if meta_path.exists() and not is_new:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        else:
            meta = {
                "session_id": session_id,
                "workdir": str(self._workdir),
                "created_at": now,
            }

        meta["updated_at"] = now
        if topic:
            meta["topic"] = topic[:100]
        if permission_mode:
            meta["permission_mode"] = permission_mode
        if pre_plan_mode:
            meta["pre_plan_mode"] = pre_plan_mode
        elif "pre_plan_mode" in meta:
            del meta["pre_plan_mode"]

        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("写入会话元数据失败: %s", e)

    def save_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """将完整对话历史写入磁盘（覆写式快照）。

        使用原子写入（先写 .tmp 再 os.replace），正确处理 compact 后历史缩短的场景。

        Args:
            session_id: 会话 UUID。
            messages: 完整的对话历史消息列表。
        """
        if not messages:
            return
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        history_path = self._sessions_dir / f"{session_id}.hist.json"
        tmp_path = history_path.with_suffix(".tmp")
        try:
            data = json.dumps(messages, ensure_ascii=False)
            tmp_path.write_text(data, encoding="utf-8")
            os.replace(tmp_path, history_path)
        except OSError as e:
            logger.warning("写入会话历史失败: %s", e)

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """列出最近的会话，按 updated_at 降序排列。

        Args:
            limit: 最多返回的会话数量。

        Returns:
            会话元数据字典列表，包含 session_id、workdir、updated_at、topic 等。
        """
        if not self._sessions_dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for f in self._sessions_dir.glob("*.json"):
            if f.name.endswith(".hist.json"):
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                if "session_id" in meta:
                    sessions.append(meta)
            except (json.JSONDecodeError, OSError):
                continue
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions[:limit]

    def load_history(self, session_id: str) -> list[dict[str, Any]]:
        """加载指定会话的完整对话历史。

        Args:
            session_id: 目标会话 UUID。

        Returns:
            消息列表，每条为标准 role/content 字典。文件不存在时返回空列表。
        """
        history_path = self._sessions_dir / f"{session_id}.hist.json"
        if not history_path.exists():
            return []
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("加载会话历史失败: %s", e)
        return []

    def resolve_resume(
        self,
        cmd_args: list[str],
        current_session_id: str,
        current_workdir: str,
    ) -> str | ResumeResult:
        """处理 /resume 命令的会话解析与加载逻辑。

        无参数时返回格式化的会话列表文本；有参数时解析目标、加载历史、校验工作目录。
        Agent 侧只需判断返回类型：str 直接输出，ResumeResult 应用状态变更。

        Args:
            cmd_args: 命令参数列表，可为空（列出会话）、序号或 session_id 前缀。
            current_session_id: 当前会话 ID，用于从列表中排除。
            current_workdir: 当前工作目录路径字符串，用于校验一致性。

        Returns:
            str: 要显示给用户的文本（会话列表或错误信息）。
            ResumeResult: 成功解析的会话数据，调用方负责应用到 Agent 状态。
        """
        sessions = self.list_sessions(limit=10)
        sessions = [s for s in sessions if s.get("session_id") != current_session_id]

        if not sessions:
            return "没有可恢复的历史会话。\n"

        if not cmd_args:
            lines = ["最近的历史会话：\n"]
            for i, s in enumerate(sessions, 1):
                updated = s.get("updated_at", "?")[:19].replace("T", " ")
                topic = s.get("topic", "")
                workdir = s.get("workdir", "")
                lines.append(f"  {i}. [{updated}] {workdir}\n     {topic}\n")
            lines.append("输入 /resume <序号> 恢复指定会话。\n")
            return "\n".join(lines)

        # 解析目标会话：先尝试序号，再尝试 session_id 前缀匹配
        target_arg = cmd_args[0]
        target_session: dict | None = None

        try:
            idx = int(target_arg)
            if 1 <= idx <= len(sessions):
                target_session = sessions[idx - 1]
        except ValueError:
            pass

        if target_session is None:
            for s in sessions:
                sid = s.get("session_id", "")
                if sid == target_arg or sid.startswith(target_arg):
                    target_session = s
                    break

        if target_session is None:
            return f"未找到匹配的会话: {target_arg}\n"

        target_id = target_session["session_id"]

        messages = self.load_history(target_id)
        if not messages:
            return f"会话 {target_id[:8]}... 没有保存的对话历史。\n"

        # 工作目录不一致时拒绝恢复
        saved_workdir = target_session.get("workdir", "")
        if saved_workdir and current_workdir and saved_workdir != current_workdir:
            return (
                f"无法恢复：原会话工作目录为 {saved_workdir}，当前为 {current_workdir}。\n"
                f"请在原工作目录下启动后再恢复该会话。\n"
            )

        return ResumeResult(
            session_id=target_id,
            messages=messages,
            metadata=target_session,
        )

    def get_metadata(self, session_id: str) -> dict[str, Any] | None:
        """获取指定会话的元数据。

        Args:
            session_id: 目标会话 UUID。

        Returns:
            元数据字典，不存在时返回 None。
        """
        meta_path = self._sessions_dir / f"{session_id}.json"
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
