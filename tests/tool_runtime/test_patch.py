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


def test_patch_line_without_prefix_is_invalid_arguments(runtime, tmp_path):
    """缺少行首标记的补丁行在参数校验期被拒，不会误报成权限拒绝。

    回归点：补丁结构错误原先只在 PermissionManager 计算补丁路径时抛 ValueError，
    被统一映射为 permission_denied，模型据此以为是自己没有写权限。
    """
    (tmp_path / 'a').write_text('hello\n')
    result = apply(runtime, '*** Begin Patch\n*** Update File: a\n@@\nhello\n*** End Patch')
    assert result.error_code == 'invalid_arguments'
    assert '补丁行必须以' in result.text
    assert (tmp_path / 'a').read_text() == 'hello\n'


def test_patch_field_name_must_be_patch(runtime, tmp_path):
    """字段名写错（如 input）返回 invalid_arguments 并指出合法字段。"""
    deps, agent = runtime
    agent.mode = RunMode.EXECUTE
    result = asyncio.run(deps.tools_mgr.execute(
        'apply_patch', {'input': '*** Begin Patch\n*** Add File: a\n+hello\n*** End Patch'}, deps=deps, agent=agent,
    ))
    assert result.error_code == 'invalid_arguments'
    assert result.error_details['allowed_fields'] == ['patch']
    assert not (tmp_path / 'a').exists()
