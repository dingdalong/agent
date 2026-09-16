import asyncio
import pytest
from src.mode import RunMode


def apply(runtime, patch):
    deps, agent = runtime
    agent.mode = RunMode.EXECUTE
    return asyncio.run(deps.tools_mgr.execute('apply_patch', {'patch': patch}, deps=deps, agent=agent))


def test_create_update_move_and_delete(runtime, tmp_path):
    result = apply(runtime, '*** Begin Patch\n*** Add File: a.txt\n+hello\n+world\n*** End Patch')
    assert result.status == 'success', str(result)
    result = apply(runtime, '*** Begin Patch\n*** Update File: a.txt\n*** Move to: dir/b.txt\n@@\n hello\n-world\n+there\n*** End Patch')
    assert result.status == 'success', str(result)
    assert (tmp_path / 'dir/b.txt').read_text() == 'hello\nthere\n'
    assert not (tmp_path / 'a.txt').exists()
    assert apply(runtime, '*** Begin Patch\n*** Delete File: dir/b.txt\n*** End Patch').status == 'success'
    assert not (tmp_path / 'dir/b.txt').exists()


def test_ambiguous_patch_changes_nothing(runtime, tmp_path):
    (tmp_path / 'a').write_text('same\nsame\n')
    result = apply(runtime, '*** Begin Patch\n*** Add File: b\n+new\n*** Update File: a\n@@\n-same\n+other\n*** End Patch')
    assert result.error_code == 'patch_failed'
    assert (tmp_path / 'a').read_text() == 'same\nsame\n'
    assert not (tmp_path / 'b').exists()


def test_patch_preserves_line_endings_and_mode(runtime, tmp_path):
    path = tmp_path / 'a'
    path.write_bytes(b'one\r\ntwo\r\n')
    path.chmod(0o750)
    result = apply(runtime, '*** Begin Patch\n*** Update File: a\n@@\n-one\n+ONE\n*** End Patch')
    assert result.status == 'success'
    assert path.read_bytes() == b'ONE\r\ntwo\r\n'
    assert path.stat().st_mode & 0o777 == 0o750


def test_plan_cannot_patch(runtime, tmp_path):
    deps, agent = runtime
    result = asyncio.run(deps.tools_mgr.execute('apply_patch', {'patch': '*** Begin Patch\n*** Add File: .agent/plans/a\n+forbidden\n*** End Patch'}, deps=deps, agent=agent))
    assert result.error_code == 'tool_unavailable'
    assert not (tmp_path / '.agent').exists()
