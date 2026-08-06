"""项目可执行配置加载前的工作区信任门。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from src.mgr.secure_io import atomic_write_text


TrustConfirmation = Callable[[str], Awaitable[bool]]


@dataclass
class ProjectTrustGate:
    workdir: Path
    global_dir: Path

    def __post_init__(self) -> None:
        self.workdir = self.workdir.expanduser().resolve(strict=True)
        self.global_dir = self.global_dir.expanduser().resolve(strict=False)
        self.store_path = self.global_dir / "trusted_projects.json"

    async def ensure_trusted(
        self,
        confirm: TrustConfirmation | None = None,
    ) -> bool:
        """确认未知工作目录并记录信任；缺少可用确认通道时默认拒绝。

        用户取消和普通确认异常按拒绝处理；调用任务取消与进程退出继续传播。
        """
        trusted_workdirs = await asyncio.to_thread(self._load_trusted_workdirs)
        key = str(self.workdir)
        if key in trusted_workdirs:
            return True
        if confirm is None or not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        prompt = (
            f"项目 {self.workdir} 包含可执行配置或凭据加载入口。\n"
            "是否信任该工作目录并加载项目环境、模型端点、Hook、Plugin Hook 和 MCP？"
        )
        try:
            accepted = await confirm(prompt)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return False
        except (EOFError, KeyboardInterrupt):
            return False
        except Exception:
            return False
        if accepted is not True:
            return False
        trusted_workdirs.add(key)
        await asyncio.to_thread(self._save_trusted_workdirs, trusted_workdirs)
        return True

    def _load_trusted_workdirs(self) -> set[str]:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(data, list):
            return set()
        return {item for item in data if isinstance(item, str)}

    def _save_trusted_workdirs(self, trusted_workdirs: set[str]) -> None:
        content = json.dumps(sorted(trusted_workdirs), ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(self.store_path, content)
