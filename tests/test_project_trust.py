from __future__ import annotations

import asyncio
import json
import os
import pty
import select
import subprocess
import sys
import termios
import textwrap
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.app.app import AgentApp
from src.app.bootstrap import _confirm_project_trust
from src.interfaces.agent_view_store import AgentViewStore
from src.mgr.config_mgr import ConfigManager
from src.mgr.data_guard import DataGuard, REDACTED, register_runtime_secrets
from src.mgr.project_trust import ProjectTrustGate

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_known_workdir_is_trusted_without_prompt(tmp_path):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    global_dir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    gate.store_path.write_text(json.dumps([str(workdir.resolve())]))
    os.chmod(gate.store_path, 0o600)
    prompts = []

    async def unexpected(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    assert asyncio.run(gate.ensure_trusted(unexpected)) is True
    assert prompts == []


def test_non_tty_unknown_project_is_restricted(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert asyncio.run(gate.ensure_trusted()) is False
    assert not gate.store_path.exists()


def test_trusted_workdir_ignores_project_changes_and_store_is_owner_only(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    (workdir / ".agent").mkdir(parents=True)
    gate = ProjectTrustGate(workdir, global_dir)
    prompts = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    assert asyncio.run(gate.ensure_trusted(confirm)) is True
    (workdir / ".agent" / "settings.json").write_text('{"hooks": {}}')
    assert asyncio.run(gate.ensure_trusted(confirm)) is True
    assert len(prompts) == 1
    assert json.loads(gate.store_path.read_text()) == [str(workdir.resolve())]
    assert gate.store_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "stored",
    [
        lambda workdir: json.dumps({str(workdir.resolve()): "old-fingerprint"}),
        lambda _workdir: "{invalid",
    ],
)
def test_invalid_or_legacy_store_requires_confirmation(tmp_path, monkeypatch, stored):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    global_dir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    gate.store_path.write_text(stored(workdir))
    prompts = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    assert asyncio.run(gate.ensure_trusted(confirm)) is True
    assert len(prompts) == 1
    assert json.loads(gate.store_path.read_text()) == [str(workdir.resolve())]


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [("y", True), ("yes", True), ("", False), ("n", False)],
)
def test_startup_confirmation_accepts_plain_text_line(submitted, expected):
    async def scenario() -> bool:
        prompts: list[str] = []

        async def reader(prompt: str) -> str:
            prompts.append(prompt)
            return submitted

        result = await _confirm_project_trust("Trust? ", reader)
        assert prompts == ["Trust? [y/N] "]
        return result

    assert asyncio.run(scenario()) is expected


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt(), RuntimeError("failed")])
def test_confirmation_failure_is_restricted_without_writing(tmp_path, monkeypatch, failure):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def fail(_prompt: str) -> bool:
        raise failure

    assert asyncio.run(gate.ensure_trusted(fail)) is False
    assert not gate.store_path.exists()


@pytest.mark.parametrize("accepted", [False, None])
def test_rejected_confirmation_does_not_write(tmp_path, monkeypatch, accepted):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def reject(_prompt: str) -> bool:
        return accepted

    assert asyncio.run(gate.ensure_trusted(reject)) is False
    assert not gate.store_path.exists()


def test_unknown_project_without_confirmation_is_restricted(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert asyncio.run(gate.ensure_trusted()) is False
    assert not gate.store_path.exists()


def test_task_cancellation_during_confirmation_propagates(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def scenario() -> None:
        started = asyncio.Event()

        async def wait_forever(_prompt: str) -> bool:
            started.set()
            await asyncio.Future()
            return True

        task = asyncio.create_task(gate.ensure_trusted(wait_forever))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not gate.store_path.exists()


def test_cancelled_confirmation_without_task_cancellation_is_restricted(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def cancel_menu(_prompt: str) -> bool:
        raise asyncio.CancelledError

    assert asyncio.run(gate.ensure_trusted(cancel_menu)) is False
    assert not gate.store_path.exists()


def test_process_exit_during_confirmation_propagates(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def exit_process(_prompt: str) -> bool:
        raise SystemExit(2)

    with pytest.raises(SystemExit, match="2"):
        asyncio.run(gate.ensure_trusted(exit_process))
    assert not gate.store_path.exists()


def test_clear_uses_choice_before_reset_gate_and_defaults_to_restricted(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    trust_gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: pytest.fail("/clear must not read stdin directly"),
    )
    order: list[str] = []

    class ConfigProbe:
        project_trusted = True

        def set_project_trusted(self, trusted: bool) -> None:
            order.append(f"trusted:{trusted}")
            self.project_trusted = trusted

        def reload(self) -> None:
            order.append("reload")

    class BusProbe:
        async def request_choice(self, prompt, options, default_index, source):
            order.append("choice")
            assert "是否信任该工作目录" in prompt
            assert options == [
                ("restricted", "以受限模式继续"),
                ("trust", "信任并加载"),
            ]
            assert default_index == 0
            assert source == "project_trust"
            return ""

        def reject_ui_requests(self):
            @contextmanager
            def gate():
                order.append("gate")
                yield

            return gate()

        async def join(self):
            order.append("join")

        async def request_output(self, content):
            assert "智能体工作台" in content.plain
            order.append("banner")

    class UiProbe:
        @asynccontextmanager
        async def reset_session_interactions(self):
            order.append("ui_gate")
            yield

        async def replace_session_state(self, _state):
            order.append("replace")

    config = ConfigProbe()
    deps = SimpleNamespace(
        trust_gate=trust_gate,
        config_mgr=config,
        hooks_mgr=None,
        mcp_mgr=None,
        event_bus=BusProbe(),
        ui=UiProbe(),
        session_id="old-session",
        session_context=[],
        memory_mgr=None,
        tools_mgr=None,
        permission_mgr=None,
        plugin_mgr=None,
        plan_mgr=None,
        role_mgr=None,
        command_mgr=None,
        plan_mode_controller=None,
        workdir=workdir,
    )
    app = AgentApp(deps, AgentViewStore(), output_router=object())
    new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")

    with (
        patch("src.app.app.Agent.from_manifest", return_value=new_agent),
        patch.object(AgentApp, "_install_plan_mode_controller"),
    ):
        assert asyncio.run(app.reset_session(source="clear")) is new_agent

    assert order[:4] == ["choice", "ui_gate", "gate", "join"]
    assert order.index("trusted:False") > order.index("join")
    assert order[-3:] == ["replace", "banner", "join"]
    assert not trust_gate.store_path.exists()


def test_restricted_config_ignores_project_executable_inputs(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    project_dir = workdir / ".agent"
    project_dir.mkdir(parents=True)
    global_dir.mkdir()
    (project_dir / ".env").write_text("PROJECT_TRUST_SENTINEL=loaded\n")
    (project_dir / "config.yaml").write_text(
        "llm:\n  default: project-model\n"
        "llm_provider:\n  project:\n    api_key: project-secret\n"
    )
    (project_dir / "settings.json").write_text('{"hooks": {"PreToolUse": []}}')
    (project_dir / "mcp_servers.json").write_text(
        '{"mcpServers": {"project": {"command": "unsafe"}}}'
    )
    monkeypatch.delenv("PROJECT_TRUST_SENTINEL", raising=False)

    config = ConfigManager(global_dir, workdir, project_trusted=False)
    assert os.getenv("PROJECT_TRUST_SENTINEL") is None
    assert config.get_config("llm.default") != "project-model"
    with pytest.raises(KeyError):
        config.get_config("llm_provider.project")
    assert config.get_user_setting("hooks") == {}
    assert config.load_mcp_servers() == {}


def test_runtime_secret_registration_tracks_trusted_env(tmp_path):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    (workdir / ".agent").mkdir(parents=True)
    global_dir.mkdir()
    (workdir / ".agent" / ".env").write_text("SERVICE_TOKEN=project-secret-value\n")
    config = ConfigManager(global_dir, workdir, project_trusted=True)
    guard = DataGuard()

    register_runtime_secrets(guard, config, global_dir, workdir, True)

    assert guard.redact("project-secret-value") == REDACTED


def test_revoking_trust_removes_project_values_from_private_environment(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    project_dir = workdir / ".agent"
    project_dir.mkdir(parents=True)
    global_dir.mkdir()
    (project_dir / ".env").write_text("PROJECT_ONLY_SENTINEL=project-value\n")
    monkeypatch.delenv("PROJECT_ONLY_SENTINEL", raising=False)

    config = ConfigManager(global_dir, workdir, project_trusted=True)
    assert config.environment["PROJECT_ONLY_SENTINEL"] == "project-value"
    assert os.getenv("PROJECT_ONLY_SENTINEL") is None

    config.set_project_trusted(False)
    assert "PROJECT_ONLY_SENTINEL" not in config.environment
    assert os.getenv("PROJECT_ONLY_SENTINEL") is None


def test_read_console_line_survives_leftover_raw_mode(tmp_path):
    """终端残留 raw 模式（ICRNL 被清除）时，启动期确认仍能按回车提交。

    Textual 进入 raw 模式会清除 ICRNL；异常退出未走完恢复流程时该位残留在终端上。
    此时回车发出的 CR 不再翻译成 NL，而 canonical 模式只认 NL 作行分隔符，
    readline() 便永远等不到行结束符而卡死，ECHO 还把 CR 回显成字面量 ^M。
    """
    ready_path = tmp_path / "ready"
    go_path = tmp_path / "go"
    script = textwrap.dedent(
        f"""
        import asyncio, os, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from src.interfaces.tui.plain import read_console_line

        while not os.path.exists({str(go_path)!r}):   # 等父进程先破坏终端
            time.sleep(0.02)
        open({str(ready_path)!r}, "w").close()        # 规范化前告知父进程可以按键
        answer = asyncio.run(read_console_line("[y/N] "))
        sys.stdout.write("GOT=%r\\r\\n" % answer)
        sys.stdout.flush()
        """
    )

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=os.getcwd(),
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()

    def drain(timeout: float = 0.1) -> None:
        while select.select([master], [], [], timeout)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            output.extend(chunk)

    try:
        # 复现残留状态：只清除 ICRNL，保留 ICANON/ECHO——正是卡死时的终端状态。
        attrs = termios.tcgetattr(master)
        attrs[0] &= ~termios.ICRNL
        termios.tcsetattr(master, termios.TCSANOW, attrs)
        go_path.touch()

        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None:
            assert time.monotonic() < deadline, "子进程未就绪"
            drain(0.02)

        drain(0.5)  # 等提示写出，确保规范化已生效
        os.write(master, b"y\r")  # 用户按 y 再按回车

        while time.monotonic() < deadline and b"GOT=" not in output:
            drain()

        decoded = output.decode(errors="replace")
        assert "GOT=" in decoded, f"回车未能提交，终端输出：{decoded!r}"
        assert "'y'" in decoded, decoded
    finally:
        process.kill()
        process.wait(timeout=5)
        os.close(master)
