import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mgr.tool_output import ToolOutput
from src.tools.display import ToolResult


def test_budget_artifact_and_no_tokenizer(runtime):
    deps, agent = runtime
    agent.llm.estimate_tokens = lambda *args: pytest.fail('输出裁剪不能调用 tokenizer')
    original = '中文🙂\n' * 10000
    result = deps.tools_mgr.output.finalize(ToolResult(original), agent)
    assert result.truncated and result.artifact_complete
    assert len(str(result).encode()) <= 40000
    artifact = Path(result.artifact_path)
    assert artifact.read_text() == original
    assert result.text.startswith('中文🙂') and result.text.endswith('中文🙂\n')
    if os.name != 'nt':
        assert artifact.stat().st_mode & 0o777 == 0o600
        assert artifact.parent.stat().st_mode & 0o777 == 0o700
    deps.tools_mgr.reload()
    assert not artifact.exists()


def test_artifact_caps_eviction_and_owner_directories(runtime):
    _, agent = runtime
    output = ToolOutput({'artifact_max_bytes': 5000, 'artifact_total_bytes': 10000})
    try:
        results = [output.finalize(ToolResult(str(i) * 10000), agent, 1000) for i in range(3)]
        assert not Path(results[0].artifact_path).exists()
        assert all(not r.artifact_complete for r in results)
        assert '[中间内容已截断]' in Path(results[-1].artifact_path).read_text()
        other = SimpleNamespace(uuid='other', deps=agent.deps)
        extra = output.finalize(ToolResult('x' * 10000), other, 1000)
        assert Path(extra.artifact_path).parent != Path(results[-1].artifact_path).parent
        assert sum(p.stat().st_size for p in Path(extra.artifact_path).parent.parent.rglob('*.log')) <= 10000
    finally:
        output.clear()


def test_state_preserved_and_control_integrity(runtime, monkeypatch):
    output = runtime[0].tools_mgr.output
    agent = runtime[1]
    for status in ['success', 'error', 'running', 'cancelled']:
        result = output.finalize(ToolResult('x' * 80000, status=status, exit_code=7, error_code='cause', session_id='process'), agent)
        assert (result.status, result.exit_code, result.error_code, result.session_id) == (status, 7, 'cause', 'process')
    result = output.finalize(ToolResult('x' * 80000), agent, control=True)
    assert result.error_code == 'control_output_too_large' and result.artifact_path is None
    dropped = output.finalize(ToolResult('x' * 80000, truncated=True), agent)
    assert dropped.artifact_complete is False
    monkeypatch.setattr(output, '_save_artifact', lambda *_: (_ for _ in ()).throw(OSError('write failed')))
    failed = output.finalize(ToolResult('x' * 80000, status='error', exit_code=3), agent)
    assert failed.status == 'error' and failed.exit_code == 3
    assert failed.artifact_path is None and failed.artifact_error
    assert len(str(failed).encode()) <= 40000


def test_concurrent_artifacts_are_bounded(runtime):
    _, agent = runtime
    output = ToolOutput({'artifact_max_bytes': 5000, 'artifact_total_bytes': 10000})
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: output.finalize(ToolResult(str(i) * 10000), agent, 1000), range(20)))
        existing = [Path(r.artifact_path) for r in results if Path(r.artifact_path).exists()]
        assert sum(p.stat().st_size for p in existing) <= 10000
    finally:
        output.clear()


def test_config_budget_is_used_and_invalid_override_never_executes(runtime):
    deps, agent = runtime
    deps.tools_mgr.output = ToolOutput({'output_tokens': 1000, 'max_output_tokens': 2000})
    target = deps.workdir / 'large.txt'
    target.write_text('line\n' * 2000)
    async def scenario():
        default = await deps.tools_mgr.execute('read_file', {'path': 'large.txt'}, deps=deps, agent=agent)
        expanded = await deps.tools_mgr.execute('read_file', {'path': 'large.txt', 'max_output_tokens': 2000}, deps=deps, agent=agent)
        assert len(str(default).encode()) <= 4000
        assert len(expanded.text) > len(default.text)
        invalid = await deps.tools_mgr.execute('exec_command', {'command': 'rg --files', 'max_output_tokens': 2001}, deps=deps, agent=agent)
        assert invalid.error_code == 'invalid_arguments'
        assert not deps.process_mgr.sessions
    asyncio.run(scenario())


def test_process_increment_artifact_survives_command_completion(runtime):
    import sys
    deps, agent = runtime
    async def scenario():
        owner = (deps.session_id, str(agent.uuid))
        command = f'"{sys.executable}" -c "print(\'sentinel-secret\' * 5000)"'
        started = await deps.process_mgr.start(owner, command, deps.workdir, {}, None, 5000, 0)
        result = await deps.tools_mgr.execute('write_stdin', {'session_id': started.session_id, 'yield_time_ms': 3000}, deps=deps, agent=agent)
        assert result.status == 'success', str(result)
        assert not deps.process_mgr.sessions
        artifact = Path(result.artifact_path)
        assert artifact.exists()
        assert 'sentinel-secret' not in artifact.read_text()
        await deps.process_mgr.close()
        assert artifact.exists()
        deps.tools_mgr.reload()
        assert not artifact.exists()
        gone = await deps.tools_mgr.execute('read_file', {'path': str(artifact)}, deps=deps, agent=agent)
        assert gone.error_code == 'read_failed'
    asyncio.run(scenario())


def test_hook_modified_output_budget_is_validated_and_used(runtime):
    from src.mgr.hooks_mgr import HookRunResult
    deps, agent = runtime
    (deps.workdir / 'a').write_text('content\n' * 5000)
    class Hooks:
        budget = 256
        async def run_event(self, event, name, payload, **kwargs):
            if event == 'PreToolUse':
                return HookRunResult(updated_input={**payload['tool_input'], 'max_output_tokens': self.budget})
            return HookRunResult()
    hooks = Hooks()
    deps.hooks_mgr = hooks
    async def scenario():
        result = await deps.tools_mgr.execute('read_file', {'path': 'a'}, deps=deps, agent=agent)
        assert result.status == 'success', str(result)
        assert len(str(result).encode()) <= 1024
        hooks.budget = 16001
        result = await deps.tools_mgr.execute('read_file', {'path': 'a'}, deps=deps, agent=agent)
        assert result.error_code == 'invalid_arguments'
    asyncio.run(scenario())
