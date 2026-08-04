"""TUI 生命周期与渲染诊断日志。"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.mgr.data_guard import DataGuard


_MAX_FILE_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 2
_MAX_FIELD_LENGTH = 2048
_STOP = object()
_PROHIBITED_FIELDS = {
    "content",
    "input",
    "markdown",
    "message",
    "prompt",
    "source",
    "tool_arguments",
    "tool_args",
}


class TuiDiagnostics:
    """以后台线程写入不含会话正文的滚动 JSONL 日志。"""

    def __init__(
        self,
        directory: Path | None,
        data_guard: DataGuard | None = None,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        backup_count: int = _BACKUP_COUNT,
    ) -> None:
        self.run_id = uuid.uuid4().hex
        self.path = Path(directory) / "tui.jsonl" if directory is not None else None
        self._data_guard = data_guard
        self._max_file_bytes = max_file_bytes
        self._backup_count = backup_count
        self._queue: queue.Queue[dict[str, Any] | object] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        if self.path is not None:
            self._queue = queue.Queue()
            self._thread = threading.Thread(
                target=self._write_loop,
                name="tui-diagnostics",
                daemon=True,
            )
            self._thread.start()

    @property
    def diagnostic_id(self) -> str:
        return self.run_id[:12]

    def record(self, event: str, *, generation: int | None = None, **fields: Any) -> None:
        target = self._queue
        if target is None or self._closed:
            return
        entry: dict[str, Any] = {
            "timestamp": time.time(),
            "run_id": self.run_id,
            "event": self._safe_value(event),
        }
        if generation is not None:
            entry["generation"] = generation
        for key, value in fields.items():
            if key.lower() in _PROHIBITED_FIELDS:
                continue
            entry[key] = self._safe_value(value)
        try:
            target.put_nowait(entry)
        except Exception:
            pass

    def record_exception(
        self,
        event: str,
        error: BaseException,
        *,
        generation: int | None = None,
        **fields: Any,
    ) -> None:
        detail = "".join(traceback.format_exception(error))
        self.record(
            event,
            generation=generation,
            exception_type=type(error).__name__,
            exception_text=str(error),
            traceback=detail,
            **fields,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        target = self._queue
        thread = self._thread
        if target is None or thread is None:
            return
        try:
            target.put(_STOP)
            thread.join(timeout=5)
        except Exception:
            pass

    def _safe_value(self, value: Any) -> Any:
        try:
            redacted = (
                self._data_guard.redact(value)
                if self._data_guard is not None
                else value
            )
        except Exception:
            redacted = "<unavailable>"
        if redacted is None or isinstance(redacted, (bool, int, float)):
            return redacted
        if isinstance(redacted, str):
            return redacted[:_MAX_FIELD_LENGTH]
        if isinstance(redacted, dict):
            return {
                str(key)[:128]: self._safe_value(item)
                for key, item in redacted.items()
                if str(key).lower() not in _PROHIBITED_FIELDS
            }
        if isinstance(redacted, (list, tuple, set)):
            return [self._safe_value(item) for item in list(redacted)[:100]]
        return str(redacted)[:_MAX_FIELD_LENGTH]

    def _write_loop(self) -> None:
        target = self._queue
        path = self.path
        if target is None or path is None:
            return
        stream = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            stream = self._open_log(path)
            while True:
                item = target.get()
                if item is _STOP:
                    break
                try:
                    payload = (
                        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8", errors="replace")
                    if stream.tell() + len(payload) > self._max_file_bytes:
                        stream.close()
                        self._rotate(path)
                        stream = self._open_log(path)
                    stream.write(payload)
                    stream.flush()
                except Exception:
                    continue
        except Exception:
            while target.get() is not _STOP:
                pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    @staticmethod
    def _open_log(path: Path):
        stream = path.open("ab")
        os.chmod(path, 0o600)
        return stream

    def _rotate(self, path: Path) -> None:
        oldest = path.with_name(f"{path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            previous = path.with_name(f"{path.name}.{index}")
            if previous.exists():
                os.replace(previous, path.with_name(f"{path.name}.{index + 1}"))
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.1"))
        for index in range(1, self._backup_count + 1):
            backup = path.with_name(f"{path.name}.{index}")
            if backup.exists():
                os.chmod(backup, 0o600)
