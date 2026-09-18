import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from src.common.sandbox import ExecutionPolicy
from src.common.path_resolver import PathResolver


@pytest.mark.integration
def test_readonly_pipeline(runtime, tmp_path):
    (tmp_path / 'a file.txt').write_text('alpha\nbeta\n')
    deps, agent = runtime
    async def scenario():
        result = await deps.tools_mgr.execute('exec_command', {'cmd': "rg alpha 'a file.txt' | rg alpha", 'yield_time_ms': 1000}, deps=deps, agent=agent)
        assert result.status == 'success', str(result)
        assert result.text == 'alpha\n'
        await deps.process_mgr.close()
    asyncio.run(scenario())


def test_missing_json_fields_are_not_executed(runtime):
    deps, agent = runtime
    result = asyncio.run(deps.tools_mgr.execute('exec_command', {}, deps=deps, agent=agent))
    assert result.error_code == 'invalid_arguments'


@pytest.mark.integration
def test_rg_no_match_preserves_exit_code(runtime, tmp_path):
    (tmp_path / 'a').write_text('hello')
    deps, agent = runtime
    async def scenario():
        result = await deps.tools_mgr.execute('exec_command', {'cmd': 'rg missing a'}, deps=deps, agent=agent)
        assert result.status == 'success', str(result)
        assert result.exit_code == 1
        assert result.recovery is None
    asyncio.run(scenario())


@pytest.mark.integration
def test_session_incremental_timeout_and_owner(runtime, tmp_path):
    deps, agent = runtime
    async def scenario():
        command = f'"{sys.executable}" -u -c "import time; print(123); time.sleep(30)"'
        result = await deps.process_mgr.start(('s','a'), command, tmp_path, {}, ExecutionPolicy(command, tmp_path, tmp_path), 250, 20)
        assert result.status == 'running'
        denied = await deps.process_mgr.poll(('s','other'), result.session_id)
        assert denied.error_code == 'unknown_session'
        final = await deps.process_mgr.poll(('s','a'), result.session_id, yield_time_ms=500)
        assert final.error_code == 'timeout'
        assert (result.text + final.text).count('123') == 1
        assert not deps.process_mgr.sessions
    asyncio.run(scenario())


@pytest.mark.integration
def test_stdin_and_terminate(runtime, tmp_path):
    deps, _ = runtime
    async def scenario():
        command = f'"{sys.executable}" -u -c "import sys; [print(line.strip(), flush=True) for line in sys.stdin]"'
        result = await deps.process_mgr.start(('s','a'), command, tmp_path, {}, ExecutionPolicy(command, tmp_path, tmp_path), 5000, 100, stdin_open=True)
        answer = await deps.process_mgr.poll(('s','a'), result.session_id, 'hello\n', 200)
        assert 'hello' in answer.text
        ended = await deps.process_mgr.poll(('s','a'), result.session_id, terminate=True)
        assert ended.status == 'cancelled'
        assert not deps.process_mgr.sessions
    asyncio.run(scenario())


@pytest.mark.integration
def test_hidden_search_uses_rg_flags(runtime, tmp_path):
    (tmp_path / '.hidden').write_text('needle')
    (tmp_path / 'plain').write_text('needle')
    deps, agent = runtime
    async def scenario():
        first = await deps.tools_mgr.execute('exec_command', {'cmd': 'rg --files'}, deps=deps, agent=agent)
        second = await deps.tools_mgr.execute('exec_command', {'cmd': 'rg --files --hidden'}, deps=deps, agent=agent)
        assert '.hidden' not in first.text
        assert '.hidden' in second.text
    asyncio.run(scenario())


def test_utf8_split_between_polls():
    from src.mgr.process_mgr import ProcessMgr, ProcessSession
    async def scenario():
        manager = ProcessMgr()
        session = ProcessSession(('s', 'a'), False)
        session.task = asyncio.create_task(asyncio.Event().wait())
        manager.sessions['test'] = session
        data = '你好'.encode()
        manager._capture(session, data[:2])
        first = await manager.poll(('s', 'a'), 'test', yield_time_ms=0)
        manager._capture(session, data[2:])
        second = await manager.poll(('s', 'a'), 'test', yield_time_ms=0)
        assert first.text + second.text == '你好'
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.integration
def test_rg_without_path_searches_workdir_but_pipeline_reads_stdin(runtime):
    deps, agent = runtime
    (deps.workdir / 'sample.txt').write_text('needle\n')
    async def scenario():
        for command in ['rg -n needle', 'rg needle sample.txt | rg needle']:
            result = await deps.tools_mgr.execute('exec_command', {'cmd': command}, deps=deps, agent=agent)
            assert result.status == 'success'
            assert 'needle' in result.text
    asyncio.run(scenario())
