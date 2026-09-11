import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def read(runtime, **args):
    deps, agent = runtime
    return asyncio.run(deps.tools_mgr.execute('read_file', args, deps=deps, agent=agent))


def unnumber(body):
    return ''.join(line.split(' | ', 1)[1] for line in body.splitlines(keepends=True))


@pytest.mark.parametrize('text', ['', 'small', 'small\n', '中文🙂\r\n' * 500, 'line\n' * 500])
def test_first_read_fits_without_line_count_probe(runtime, text):
    (runtime[0].workdir / 'a').write_bytes(text.encode())
    result = read(runtime, path='a')
    assert result.status == 'success'
    assert result.file_range['eof'] and result.next_read is None
    assert unnumber(result.text) == text
    assert not result.truncated and result.artifact_path is None


@pytest.mark.parametrize('text', ['中文🙂\r\n' * 5000 + 'end', '🙂' * 20000 + '\r\nend', 'a' * 100000])
def test_continuation_covers_source_without_gaps(runtime, text):
    (runtime[0].workdir / 'a').write_bytes(text.encode())
    cursor = {'offset': 1, 'column': 0}
    chunks = []
    for _ in range(100):
        result = read(runtime, path='a', **cursor)
        assert result.status == 'success', str(result)
        assert len(str(result).encode()) <= 40000
        assert result.artifact_path is None
        chunks.append(unnumber(result.text))
        if result.next_read is None:
            break
        assert result.next_read != cursor
        cursor = result.next_read
    else:
        pytest.fail('续读没有前进')
    assert ''.join(chunks) == text


def test_range_limits_eof_and_source_changes(runtime):
    path = runtime[0].workdir / 'a'
    path.write_text('first\nsecond\nthird\n')
    result = read(runtime, path='a', offset=2, column=2, limit=1)
    assert unnumber(result.text) == 'cond\n'
    assert result.next_read == {'offset': 3, 'column': 0}
    assert not result.truncated
    path.write_text('changed\n')
    assert read(runtime, path='a', **result.next_read).file_range['eof']
    assert read(runtime, path='a', column=9000).error_code == 'invalid_arguments'


def test_redaction_preserves_source_positions_and_covers_multiline_secret(runtime):
    deps, _ = runtime
    secret = 'top-secret\nsecond-secret'
    deps.data_guard.register_secret(secret)
    text = 'prefix\n' + secret + '\nneedle sentinel-secret\n'
    (deps.workdir / 'a').write_text(text)
    result = read(runtime, path='a', offset=2, limit=2)
    safe = unnumber(result.text)
    assert 'top-secret' not in safe and 'second-secret' not in safe
    assert len(safe) == len(secret + '\n')
    assert result.next_read == {'offset': 4, 'column': 0}
    assert result.file_range['total_lines'] == 4


def test_hook_context_does_not_corrupt_file_positions(runtime):
    deps, _ = runtime
    (deps.workdir / 'a').write_text('data\n' * 5000)
    class Hooks:
        async def run_event(self, name, *args, **kwargs):
            return SimpleNamespace(blocked=False, permission_decisions=[], updated_input=None, additional_context=['note\n' * 50000] if name == 'PostToolUse' else [])
    deps.hooks_mgr = Hooks()
    result = read(runtime, path='a')
    assert result.status == 'success', str(result)
    assert result.next_read['offset'] == len(result.text.splitlines()) + 1
    assert len(str(result).encode()) <= 40000
    assert result.annotations and result.truncated
