"""项目可执行配置加载前的工作区信任门。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
        self.trusted = False

    async def ensure_trusted(
        self,
        confirm: TrustConfirmation | None = None,
    ) -> bool:
        """确认未知指纹并记录信任；缺少可用确认通道时默认拒绝。

        用户取消和普通确认异常按拒绝处理；调用任务取消与进程退出继续传播。
        """
        before = self.fingerprint()
        records = self._load_records()
        key = str(self.workdir)
        if records.get(key) == before:
            self.trusted = True
            return True
        self.trusted = False
        if confirm is None or not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        prompt = (
            f"项目 {self.workdir} 包含可执行配置或凭据加载入口。\n"
            "是否信任当前指纹并加载项目环境、模型端点、Hook、Plugin Hook 和 MCP？"
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
        after = self.fingerprint()
        if after != before:
            return False
        records[key] = after
        self._save_records(records)
        self.trusted = True
        return True

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in self._controlled_files():
            relative = path.relative_to(self.workdir).as_posix()
            digest.update(relative.encode())
            try:
                info = path.lstat()
            except OSError:
                continue
            digest.update(str(info.st_mode).encode())
            if path.is_symlink():
                digest.update(b"<symlink>")
                digest.update(os.readlink(path).encode(errors="replace"))
                try:
                    target = path.resolve(strict=True)
                    if target.is_file():
                        digest.update(b"<target:file>")
                        with target.open("rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                    else:
                        digest.update(b"<target:non-file>")
                except FileNotFoundError:
                    digest.update(b"<target:dangling>")
                except OSError:
                    digest.update(b"<target:unreadable>")
            elif path.is_file():
                try:
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                except OSError:
                    digest.update(b"<unreadable>")
        return digest.hexdigest()

    def _controlled_files(self) -> list[Path]:
        candidates = [
            self.workdir / ".env",
            self.workdir / ".agent" / ".env",
            self.workdir / ".agent" / "config.yaml",
            self.workdir / ".agent" / "settings.json",
            self.workdir / ".agent" / "mcp_servers.json",
        ]
        patterns = (
            ".agent/roles/*/mcp_servers.json",
            ".agent/plugins/*/hooks/hooks.json",
            ".agent/roles/*/plugins/*/hooks/hooks.json",
        )
        for pattern in patterns:
            candidates.extend(self.workdir.glob(pattern))
        return sorted({path for path in candidates if path.exists() or path.is_symlink()})

    def _load_records(self) -> dict[str, str]:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    def _save_records(self, records: dict[str, str]) -> None:
        content = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(self.store_path, content)
