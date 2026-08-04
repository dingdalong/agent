"""TUI 诊断日志的安全与轮转边界测试。"""

from __future__ import annotations

import json
import stat
from typing import Any

from src.interfaces.tui.diagnostics import TuiDiagnostics


SECRET = "diagnostic-secret-value"
REDACTED = "[REDACTED]"


class _Guard:
    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(SECRET, REDACTED)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.redact(item) for item in value]
        return value


def _entries(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_diagnostics_redacts_limits_fields_and_uses_owner_permissions(tmp_path) -> None:
    secret = SECRET
    diagnostics = TuiDiagnostics(tmp_path / "logs", _Guard())  # type: ignore[arg-type]
    try:
        diagnostics.record(
            "render",
            generation=2,
            detail=secret + "x" * 3000,
            content="conversation body must not be logged",
            markdown="## private",
        )
        try:
            raise RuntimeError(f"failed with {secret}")
        except RuntimeError as exc:
            diagnostics.record_exception("failure", exc, generation=2)
    finally:
        diagnostics.close()

    path = tmp_path / "logs" / "tui.jsonl"
    entries = _entries(path)
    assert [entry["event"] for entry in entries] == ["render", "failure"]
    assert entries[0]["run_id"] == diagnostics.run_id
    assert entries[0]["generation"] == 2
    assert secret not in path.read_text()
    assert REDACTED in entries[0]["detail"]
    assert len(entries[0]["detail"]) <= 2048
    assert "content" not in entries[0]
    assert "markdown" not in entries[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_diagnostics_rotates_to_two_backups(tmp_path) -> None:
    diagnostics = TuiDiagnostics(
        tmp_path,
        _Guard(),  # type: ignore[arg-type]
        max_file_bytes=400,
        backup_count=2,
    )
    for index in range(50):
        diagnostics.record("render", index=index, detail="x" * 80)
    diagnostics.close()

    files = sorted(tmp_path.glob("tui.jsonl*"))
    assert [path.name for path in files] == [
        "tui.jsonl",
        "tui.jsonl.1",
        "tui.jsonl.2",
    ]
    assert all(path.stat().st_size > 0 for path in files)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def test_diagnostics_write_failure_never_escapes(tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    diagnostics = TuiDiagnostics(blocked / "logs", _Guard())  # type: ignore[arg-type]
    diagnostics.record("app_started", detail="ignored")
    diagnostics.close()
