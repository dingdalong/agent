"""先验证所有文件，再提交经过授权的变更。"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile

from src.mgr.path_resolver import PathResolver, ResolvedPath
from src.tools.policy import PathRole
from src.tools.display import ToolResult, ToolDisplay, build_file_diff, format_result


@dataclass
class FilePatch:
    operation: str
    path: str
    destination: str | None = None
    chunks: list[list[str]] = field(default_factory=list)


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    if not lines or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('补丁需要 *** Begin Patch 和 *** End Patch')
    result = []
    current = None
    for line in lines[1:-1]:
        if line.startswith(('*** Add File: ', '*** Update File: ', '*** Delete File: ')):
            operation, path = line[4:].split(' File: ', 1)
            if not path.strip():
                raise ValueError('文件路径不能为空')
            current = FilePatch(operation, path, chunks=[[]])
            result.append(current)
        elif current is None:
            raise ValueError('补丁内容缺少文件头')
        elif line.startswith('*** Move to: ') and current.operation == 'Update':
            current.destination = line[len('*** Move to: '):]
        elif line.startswith('@@') and current.operation == 'Update':
            if current.chunks[-1]:
                current.chunks.append([])
        elif line == '*** End of File':
            # 文件尾锚点进入匹配，而不是行号。
            current.chunks[-1].append(line)
        elif line[:1] in {' ', '+', '-'} and current.operation != 'Delete':
            if current.operation == 'Add' and not line.startswith('+'):
                raise ValueError('Add File 的内容必须以 + 开头')
            current.chunks[-1].append(line)
        else:
            raise ValueError(f'无效补丁行：{line[:100]}')
    if not result:
        raise ValueError('补丁没有文件操作')
    return result


def prepare_patch(text: str, resolver: PathResolver, compute=True):
    patches = parse_patch(text)
    paths = []
    seen = set()
    for index, patch in enumerate(patches):
        for suffix, value in [('source', patch.path), ('destination', patch.destination)]:
            if value is None:
                continue
            target = resolver.resolve(value)
            if target in seen:
                raise ValueError(f'重复的补丁目标：{value}')
            seen.add(target)
            paths.append(ResolvedPath(f'{index}_{suffix}', PathRole.WRITE, value, target, resolver.classify(target), target.exists()))
    if not compute:
        return paths
    operations = []
    for index, patch in enumerate(patches):
        source = resolver.resolve(patch.path)
        destination = resolver.resolve(patch.destination) if patch.destination else source
        if patch.operation == 'Add':
            if source.exists():
                raise ValueError(f'目标已存在：{source}')
            old = None
            new = ('\n'.join(line[1:] for chunk in patch.chunks for line in chunk) + '\n').encode()
        else:
            resolver.validate_local_read(source)
            old = source.read_bytes()
            old.decode('utf-8')  # 补丁只修改文本；删除同样验证格式。
            if patch.operation == 'Delete':
                new = None
            else:
                text = old.decode('utf-8')
                newline = '\r\n' if '\r\n' in text else '\n'
                lines = text.splitlines()
                for chunk in patch.chunks:
                    eof = bool(chunk and chunk[-1] == '*** End of File')
                    chunk = [line for line in chunk if line != '*** End of File']
                    before = [line[1:] for line in chunk if line.startswith((' ', '-'))]
                    after = [line[1:] for line in chunk if line.startswith((' ', '+'))]
                    if not before:
                        if lines and not eof:
                            raise ValueError('无上下文插入必须使用文件尾锚点')
                        at = len(lines)
                    else:
                        candidates = []
                        for strip in (False, True):
                            candidates = [i for i in range(len(lines) - len(before) + 1)
                                          if (not eof or i + len(before) == len(lines))
                                          and ([v.rstrip() for v in lines[i:i+len(before)]] == [v.rstrip() for v in before] if strip else lines[i:i+len(before)] == before)]
                            if candidates:
                                break
                        if len(candidates) != 1:
                            raise ValueError(f'{source}: 上下文匹配 {len(candidates)} 处，请提供唯一上下文')
                        at = candidates[0]
                    lines[at:at + len(before)] = after
                new = (newline.join(lines) + (newline if text.endswith(('\n', '\r')) else '')).encode('utf-8')
                if destination != source and destination.exists():
                    raise ValueError(f'移动目标已存在：{destination}')
        operations.append((source, destination, old, new))
    return operations


def apply_patch(text, resolver, authorization):
    if not authorization.allowed:
        return ToolResult.failure('permission_denied', '补丁未授权')
    completed = []
    displays = []
    try:
        paths = prepare_patch(text, resolver, False)
        grants = {g.argument: g for g in authorization.path_grants}
        for item in paths:
            resolver.revalidate(grants[item.argument], item.original)
        operations = prepare_patch(text, resolver)
        for source, destination, old, new in operations:
            for item in paths:
                resolver.revalidate(grants[item.argument], item.original)
            current = source.read_bytes() if source.exists() else None
            if current != old:
                raise ValueError(f'文件在校验后发生变化：{source}')
            if destination != source and destination.exists():
                raise ValueError(f"移动目标在校验后出现：{destination}")
            if new is None:
                source.unlink()
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                mode = source.stat().st_mode & 0o777 if old is not None else 0o644
                fd, temporary = tempfile.mkstemp(dir=destination.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(new)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, mode)
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                if source != destination:
                    source.unlink()
            completed.append(str(destination))
            diff = build_file_diff((old or b"").decode().splitlines(), (new or b"").decode().splitlines(), str(destination))
            displays.append(diff.title + "\n" + diff.content)
        content, truncated = format_result('\n'.join(displays))
        return ToolResult('已修改：\n' + '\n'.join(completed), display=ToolDisplay('应用补丁', content, 'diff', truncated))
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        return ToolResult.failure('patch_failed', f'{exc}\n已完成：{completed or "无"}')
