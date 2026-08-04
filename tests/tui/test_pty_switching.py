"""真实 PTY 中的 Textual 方向键切换回归。"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios
import textwrap
import time


def test_vscode_pty_rapid_arrow_switching_stays_stable(tmp_path) -> None:
    ready_path = tmp_path / "ready"
    result_path = tmp_path / "result.json"
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys
        from pathlib import Path

        from src.events.types import SubagentLifecycle
        from src.interfaces.agent_view_store import AgentViewStore
        from src.interfaces.turn_clock import TurnClock
        from src.interfaces.tui.app import AgentTuiApp

        ready_path = Path(sys.argv[1])
        result_path = Path(sys.argv[2])

        class PtyApp(AgentTuiApp):
            CSS_PATH = Path("src/interfaces/tui/agent.tcss").resolve()

            def __init__(self, store):
                super().__init__(
                    store,
                    [],
                    TurnClock(),
                    lambda: None,
                    lambda: False,
                    lambda: None,
                    native_clipboard=False,
                )
                self.switch_count = 0

            async def on_ready(self):
                await self.open_transcript(
                    "worker-0",
                    ["worker-0", "worker-1"],
                    invoked=False,
                )
                await asyncio.to_thread(ready_path.write_text, "ready")

            def switch_transcript(self, delta):
                super().switch_transcript(delta)
                self.switch_count += 1
                if self.switch_count == 100:
                    asyncio.create_task(self.finish_test())

            async def finish_test(self):
                for _ in range(500):
                    if (
                        self._transcript_pending is None
                        and self._transcript_active_renders == 0
                        and self._rendered_transcript_id == self.viewing_agent_id
                    ):
                        break
                    await asyncio.sleep(0.01)
                payload = {
                    "crash_count": int(self.fatal_error is not None),
                    "target": self.viewing_agent_id,
                    "max_concurrent_renders": self._transcript_max_concurrent_renders,
                }
                await asyncio.to_thread(result_path.write_text, json.dumps(payload))
                self.exit()

        store = AgentViewStore()
        for uuid in ("worker-0", "worker-1"):
            store.record(SubagentLifecycle(
                timestamp=1.0,
                source="pty-test",
                agent_uuid=uuid,
                agent_type="worker",
                phase="end",
                messages=[{"role": "assistant", "content": uuid}],
            ))
        store.flush_completed()
        PtyApp(store).run()
        """
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    flags = fcntl.fcntl(master, fcntl.F_GETFL)
    fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    env = os.environ.copy()
    env.update({"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"})
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready_path), str(result_path)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=os.getcwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave)
    terminal_output = bytearray()

    def drain_output() -> None:
        try:
            while chunk := os.read(master, 65536):
                terminal_output.extend(chunk)
        except BlockingIOError:
            pass
        except OSError:
            pass

    try:
        deadline = time.monotonic() + 8
        while not ready_path.exists() and process.poll() is None:
            assert time.monotonic() < deadline, "PTY app did not become ready"
            drain_output()
            time.sleep(0.02)
        drain_output()
        assert process.poll() is None, terminal_output.decode(errors="replace")
        os.write(master, b"\x1b[C\x1b[D" * 50)
        while process.poll() is None:
            assert time.monotonic() < deadline, "PTY app did not process arrow keys"
            drain_output()
            time.sleep(0.02)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)

    result = json.loads(result_path.read_text())
    assert result == {
        "crash_count": 0,
        "target": "worker-0",
        "max_concurrent_renders": 1,
    }
