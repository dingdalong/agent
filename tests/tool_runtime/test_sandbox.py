"""权限测试不启动进程；真实隔离测试仅进入 integration。"""
import asyncio
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile

import pytest

from src.mgr.sandbox import ExecutionPolicy, SandboxBackend, SandboxError
from src.mgr.process_mgr import ProcessMgr
from src.mgr.features import ALL_FEATURES
from src.mgr.permission_mgr import ToolAuthorizationRequest, ToolCallerContext
from src.mode import RunMode
from src.tools import AccessKind, DataFlow, ToolOrigin, ToolPolicy
from src.tools.policy import DEFAULT_AVAILABILITY


def authorize(runtime, cmd='x=1; echo "$x"', extra=None):
    deps, agent = runtime
    request = ToolAuthorizationRequest(
        tool_name='exec_command',
        policy=ToolPolicy(AccessKind.REVIEW, DataFlow.DYNAMIC),
        availability=DEFAULT_AVAILABILITY,
        arguments={'cmd': cmd, 'additional_permissions': extra, 'justification': '测试扩权'},
        origin=ToolOrigin('builtin'),
        caller=ToolCallerContext(
            mode=agent.mode, agent_type='main', is_subagent=False,
            features=frozenset(ALL_FEATURES), declared_tools=None,
            unavailable_tools=(),
        ),
        user_intent='测试',
    )
    return asyncio.run(deps.permission_mgr.authorize(request))


def test_shell_permissions_are_bound_to_call_and_mode(runtime, tmp_path):
    deps, agent = runtime
    first = authorize(runtime)
    assert first.allowed and not first.execution_policy.writable_roots
    with pytest.raises(SandboxError):
        first.execution_policy.validate('another command', tmp_path)
    assert not authorize(runtime, extra={'network': True}).allowed
    agent.mode = RunMode.EXECUTE
    second = authorize(runtime)
    assert second.allowed and second.execution_policy.writes_workspace
    assert not first.execution_policy.writes_workspace
    assert not authorize(runtime, extra={'network': True}).allowed  # 没有审核器不会扩权


def test_directory_replacement_invalidates_policy(tmp_path):
    directory = tmp_path / 'root'
    directory.mkdir()
    policy = ExecutionPolicy('true', directory, directory, (directory,))
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SandboxError):
        policy.validate('true', directory)


def test_missing_backend_never_executes(tmp_path):
    backend = SandboxBackend(shell='/bin/sh')
    backend.system = 'unsupported'
    with pytest.raises(SandboxError, match='仅支持'):
        with backend.prepare(ExecutionPolicy('touch sentinel', tmp_path, tmp_path), {}):
            pytest.fail('不可运行')
    assert not (tmp_path / 'sentinel').exists()


def run_shell(tmp_path, command, writable=False, network=False, timeout=5000):
    async def run():
        manager = ProcessMgr(sandbox=SandboxBackend())
        policy = ExecutionPolicy(command, tmp_path, tmp_path, (tmp_path,) if writable else (), network)
        try:
            result = await manager.start(('s', 'a'), command, tmp_path, dict(os.environ), policy, timeout, 1000)
            output = result.text
            while result.session_id:
                result = await manager.poll(('s','a'), result.session_id)
                output += result.text
            result.text = output
            return result
        finally:
            await manager.close()
    return asyncio.run(run())


@pytest.mark.integration
@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='Shell 后端只支持 macOS/Linux')
@pytest.mark.parametrize('command, expected, code', [
    ('x=hello; f() { printf "%s" "$1"; }; f "$(printf "$x")"', 'hello', 0),
    ('false | printf ok', 'ok', 0),
    ('printf x; exit 7', 'x', 7),
    ("cat <<'EOF'\nhello\nEOF", 'hello\n', 0),
])
def test_real_shell_semantics(tmp_path, command, expected, code):
    result = run_shell(tmp_path, command)
    assert result.error_code is None, str(result)
    assert result.text == expected and result.exit_code == code
    assert result.status == 'success'


@pytest.mark.integration
@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='Shell 后端只支持 macOS/Linux')
def test_plan_write_network_and_execute_protection(tmp_path):
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'config').write_text('sentinel')
    before = set(tmp_path.iterdir())
    denied = run_shell(tmp_path, 'echo changed > ordinary')
    assert denied.error_code is None, str(denied)
    assert denied.exit_code != 0 and set(tmp_path.iterdir()) == before
    written = run_shell(tmp_path, 'echo ok > ordinary; echo bad > .git/config', writable=True)
    assert written.error_code is None, str(written)
    assert written.exit_code != 0
    assert (tmp_path / 'ordinary').read_text() == 'ok\n'
    assert (tmp_path / '.git' / 'config').read_text() == 'sentinel'
    command = shlex.join([sys.executable, '-c', 'import socket; socket.create_connection(("127.0.0.1", 9), timeout=1)'])
    denied = run_shell(tmp_path, command)
    assert denied.error_code is None and denied.exit_code != 0, str(denied)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='Shell 后端只支持 macOS/Linux')
def test_scratch_removed_and_project_symlink_cannot_escape(tmp_path):
    result = run_shell(tmp_path, 'echo ok > "$TMPDIR/probe"; printf "%s" "$TMPDIR"')
    assert result.exit_code == 0, str(result)
    assert not Path(result.text).exists()
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (tmp_path / 'link').symlink_to(outside, target_is_directory=True)
    result = run_shell(tmp_path, 'echo bad > link/sentinel', writable=True)
    assert result.exit_code != 0 and not (outside / 'sentinel').exists(), str(result)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='Shell 后端只支持 macOS/Linux')
def test_invalid_shell_syntax_is_program_output(tmp_path):
    result = run_shell(tmp_path, 'printf "')
    assert result.error_code is None and result.exit_code != 0, str(result)
    assert result.text


def test_directory_inode_replacement_invalidates_authorization(tmp_path):
    directory = tmp_path / 'root'
    directory.mkdir()
    policy = ExecutionPolicy('true', directory, directory, (directory,))
    directory.rename(tmp_path / 'old')
    directory.mkdir()
    with pytest.raises(SandboxError, match='替换'):
        policy.validate('true', directory)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='Shell 后端只支持 macOS/Linux')
def test_network_grant_does_not_open_host_unix_socket(tmp_path):
    # 有监听器，确保拒绝源于隔离而非端口不存在。
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        code = f'import socket; socket.create_connection(("127.0.0.1", {listener.getsockname()[1]}), timeout=1)'
        command = shlex.join([sys.executable, '-c', code])
        blocked = run_shell(tmp_path, command)
        assert blocked.error_code is None and blocked.exit_code != 0, str(blocked)
        allowed = run_shell(tmp_path, command, network=True)
        assert allowed.exit_code == 0, str(allowed)
    with tempfile.TemporaryDirectory(dir="/tmp") as short_dir, socket.socket(socket.AF_UNIX) as listener:
        address = str(Path(short_dir) / "host.sock")
        listener.bind(address)
        listener.listen()
        code = f'import socket; s=socket.socket(socket.AF_UNIX); s.connect({address!r})'
        blocked = run_shell(tmp_path, shlex.join([sys.executable, '-c', code]), network=True)
        assert blocked.error_code is None and blocked.exit_code != 0, str(blocked)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != 'linux', reason='Linux 描述符挂载契约')
def test_linux_mount_handles_are_not_inherited(tmp_path):
    code = 'import os; print([f for f in os.listdir("/proc/self/fd") if int(f)>2 and os.path.exists("/proc/self/fd/"+f)])'
    result = run_shell(tmp_path, shlex.join([sys.executable, '-c', code]), writable=True)
    assert result.exit_code == 0 and result.text.strip() == '[]', str(result)


def test_plan_cannot_feed_previous_writable_process():
    from src.mgr.process_mgr import ProcessSession
    async def run():
        manager = ProcessMgr()
        manager.sessions['old'] = ProcessSession(('s', 'a'), False)
        result = await manager.poll(('s', 'a'), 'old', 'touch file', mode=RunMode.PLAN)
        assert result.error_code == 'permission_denied'
        await manager.close()
    asyncio.run(run())
