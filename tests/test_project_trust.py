from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.app.app import AgentApp
from src.app.bootstrap import _confirm_project_trust
from src.interfaces.agent_view_store import AgentViewStore
from src.mgr.config_mgr import ConfigManager
from src.mgr.data_guard import DataGuard, REDACTED, register_runtime_secrets
from src.mgr.project_trust import ProjectTrustGate


def test_trust_fingerprint_changes_with_controlled_file(tmp_path):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    (workdir / ".agent").mkdir(parents=True)
    gate = ProjectTrustGate(workdir, global_dir)
    first = gate.fingerprint()
    (workdir / ".agent" / "settings.json").write_text('{"hooks": {}}')
    second = gate.fingerprint()
    assert first != second


def test_known_fingerprint_is_trusted_without_prompt(tmp_path):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    workdir.mkdir()
    global_dir.mkdir()
    gate = ProjectTrustGate(workdir, global_dir)
    gate.store_path.write_text(json.dumps({str(workdir.resolve()): gate.fingerprint()}))
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


def test_changed_fingerprint_requires_confirmation_and_store_is_owner_only(tmp_path, monkeypatch):
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
    assert len(prompts) == 2
    assert gate.store_path.stat().st_mode & 0o777 == 0o600


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


def test_confirmation_rechecks_fingerprint_before_writing(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    project_dir = workdir / ".agent"
    project_dir.mkdir(parents=True)
    gate = ProjectTrustGate(workdir, global_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def change_project(_prompt: str) -> bool:
        (project_dir / "settings.json").write_text('{"hooks": {}}')
        return True

    assert asyncio.run(gate.ensure_trusted(change_project)) is False
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
            assert "是否信任当前指纹" in prompt
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

    class UiProbe:
        @asynccontextmanager
        async def reset_session_interactions(self):
            order.append("ui_gate")
            yield

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
        plan_mode_controller=None,
    )
    app = AgentApp(deps, AgentViewStore(), output_router=object())
    new_agent = SimpleNamespace(uuid="new-agent", agent_type="main")

    with (
        patch("src.app.app.Agent.from_manifest", return_value=new_agent),
        patch.object(AgentApp, "_install_plan_mode_controller"),
    ):
        assert asyncio.run(app._reset_session(source="clear")) is new_agent

    assert order[:4] == ["choice", "ui_gate", "gate", "join"]
    assert order.index("trusted:False") > order.index("join")
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


def test_symlink_target_content_changes_project_fingerprint(tmp_path):
    workdir = tmp_path / "work"
    global_dir = tmp_path / "global"
    project_dir = workdir / ".agent"
    project_dir.mkdir(parents=True)
    target = tmp_path / "settings-target.json"
    target.write_text('{"hooks": {}}')
    (project_dir / "settings.json").symlink_to(target)
    gate = ProjectTrustGate(workdir, global_dir)

    before = gate.fingerprint()
    target.write_text('{"hooks": {"PreToolUse": []}}')
    assert gate.fingerprint() != before


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
