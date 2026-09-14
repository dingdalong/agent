"""历史命令回归与 Shell 语义对照；子进程测试只进入 integration。"""
import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from src.mgr.path_resolver import PathResolver
from src.mgr.sandbox import ExecutionPolicy


@pytest.mark.integration
@pytest.mark.skipif(os.name != 'posix', reason='POSIX Shell 对照')
@pytest.mark.parametrize('cmd', [
    r'rg -n "interrupt\(" a.txt',
    'cat a.txt | head -1',
    'cat missing 2>/dev/null; echo continued',
    'cat missing 2>/dev/null && echo skipped; echo continued',
    'echo first\necho second',
    'cat missing 2>&1 | head -1',
    'cat missing 2>/dev/null 1>&2 | wc -c',
    'cat a.txt 1>&2 2>/dev/null | wc -c',
    'cat a.txt 2>&1 1>&2 | head -1',
    'cd child && cat b.txt',
    'cat *.txt',
    "cat 'literal*.txt'",
])
def test_pipeline_output_and_exit_match_shell(runtime, tmp_path, cmd):
    (tmp_path / 'a.txt').write_text('interrupt(\nsecond\n')
    (tmp_path / 'literal*.txt').write_text('literal\n')
    (tmp_path / 'child').mkdir()
    (tmp_path / 'child' / 'b.txt').write_text('child\n')
    # 使用相同 shell 与 rg 比较输出和退出码。
    from src.mgr.file_mgr import _resolve_rg
    import shlex
    compare_cmd = cmd.replace('rg ', shlex.quote(_resolve_rg()) + ' ')
    expected = subprocess.run([runtime[0].process_mgr.sandbox.shell, '-c', compare_cmd], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deps, agent = runtime
    async def run():
        result = await deps.tools_mgr.execute('exec_command', {'cmd': cmd}, deps=deps, agent=agent)
        while result.session_id:
            result = await deps.process_mgr.poll(('session', 'main'), result.session_id)
        assert result.exit_code == expected.returncode, str(result)
        normalize = lambda value: value.replace(str(tmp_path) + '/', '')
        assert normalize(result.text) == normalize(expected.stdout.decode()), str(result)
        await deps.process_mgr.close()
    asyncio.run(run())


def test_tool_schema_modes_and_removed_tools(runtime):
    deps, agent = runtime
    names = lambda active: {t['function']['name'] for t in deps.tools_mgr.get_schemas(plan_active=active)}
    assert {'submit_plan', 'exec_command', 'read_file'} <= names(True)
    assert not {'apply_patch', 'task_create', 'task_list', 'task_get', 'task_update', 'save_memory'} & names(True)
    assert 'submit_plan' not in names(False)
    assert not {'calculator', 'random', 'datetime', 'encode', 'text_stats'} & names(False)
    schema = deps.tools_mgr.get('exec_command').parameters_schema
    assert 'cmd' in schema['required'] and 'command' not in schema['properties']
    result = asyncio.run(deps.tools_mgr.execute('exec_command', {'command': 'echo never'}, deps=deps, agent=agent))
    assert result.error_code == 'invalid_arguments'
    assert 'cmd' in result.error_details['allowed_fields']
    assert not deps.process_mgr.sessions


def test_register_collision_is_explicit(runtime):
    deps, _ = runtime
    with pytest.raises(ValueError, match='名称冲突'):
        deps.tools_mgr.register(deps.tools_mgr.get('exec_command'))


def test_terminate_and_input_cannot_be_combined(runtime):
    deps, _ = runtime
    result = asyncio.run(deps.process_mgr.poll(('session', 'main'), 'unknown', chars='x', terminate=True))
    assert result.error_code == 'invalid_arguments'


def test_model_truncation_does_not_include_ui_folding(runtime):
    from src.events.types import ToolCallCompleted
    deps, agent = runtime
    events = []
    class Bus:
        async def emit(self, event):
            events.append(event)
    deps.event_bus = Bus()
    async def run():
        await deps.tools_mgr._emit_tool_completed(deps, agent, deps.tools_mgr.get('exec_command'),
            'call', 'success', 0, '\n'.join(str(i) for i in range(100)), truncated=False)
    asyncio.run(run())
    event = next(e for e in events if isinstance(e, ToolCallCompleted))
    assert event.display.truncated
    assert not event.truncated


def test_nested_unknown_fields_and_conflicting_stdin_are_rejected(runtime):
    deps, agent = runtime
    async def run():
        question = await deps.tools_mgr.execute('ask_user', {'questions': [{'question': 'x', 'typo': True}]}, deps=deps, agent=agent)
        assert question.error_code == 'invalid_arguments'
        stdin = await deps.tools_mgr.execute('write_stdin', {'session_id': 'none', 'chars': 'x', 'terminate': True}, deps=deps, agent=agent)
        assert stdin.error_code == 'invalid_arguments'
    asyncio.run(run())


@pytest.mark.integration
def test_large_pipeline_sigpipe_is_not_a_failure(runtime, tmp_path):
    (tmp_path / 'large').write_text('needle\n' * 100000)
    deps, agent = runtime
    async def run():
        result = await deps.tools_mgr.execute('exec_command', {'cmd': 'cat large | head -1'}, deps=deps, agent=agent)
        assert result.status == 'success', str(result)
        assert result.exit_code == 0
        assert result.text == 'needle\n'
        assert result.recovery is None
    asyncio.run(run())


def test_actual_agent_mode_switch_refreshes_tools_and_prompt(tmp_path):
    from tests.test_subagent_skills import _main, _child
    parent = _main(tmp_path, 'coding')
    parent.set_plan_active(False)
    assert 'apply_patch' in {s['function']['name'] for s in parent._tools_schemas}
    parent._prompt_mgr.build()
    parent.set_plan_active(True)
    assert parent._prompt_mgr._static_prefix is None
    names = {s['function']['name'] for s in parent._tools_schemas}
    assert 'apply_patch' not in names and 'submit_plan' in names
    child = _child(parent, 'coder')
    names = {s['function']['name'] for s in child._tools_schemas}
    assert not {'apply_patch', 'submit_plan', 'task_create'} & names


@pytest.mark.integration
def test_git_c_matches_git_without_changing_following_command(runtime, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'nested').mkdir()
    (repo / 'tracked.txt').write_text('x')
    subprocess.run(['git', '-C', str(repo), 'add', 'tracked.txt'], check=True)
    (tmp_path / 'outside.txt').write_text('outside\n')
    cmd = 'git -C repo -C nested -C .. ls-files -- tracked.txt 2>/dev/null; cat outside.txt'
    expected = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    deps, agent = runtime
    result = asyncio.run(deps.tools_mgr.execute('exec_command', {'cmd': cmd}, deps=deps, agent=agent))
    assert result.status == 'success', str(result)
    assert result.text == expected


def test_mcp_error_is_not_augmented():
    from types import SimpleNamespace
    from src.mgr.mcp_mgr import _format_result
    original = 'Vendor error: unknown query operator'
    result = _format_result(SimpleNamespace(isError=True, content=[SimpleNamespace(text=original)]))
    assert result.text == original
    assert result.error_code == 'mcp_error'
    assert result.recovery is None
