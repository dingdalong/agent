"""将受限 Shell AST 编译为可直接执行的 argv；不执行解析后的 Shell 文本。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil

import bashlex

from src.mgr.file_mgr import _resolve_rg
from src.mgr.path_resolver import PathResolver, PathResolutionError


@dataclass(frozen=True)
class ReadonlyCommand:
    pipelines: tuple[tuple[tuple[str, ...], ...], ...]
    paths: tuple[Path, ...]


class UnsupportedCommand(ValueError):
    pass


# 只接受列出的无副作用选项。新增选项必须同时增加授权测试。
_FLAGS = {
    'ls': {'-a', '-l', '-la', '-al', '-1', '-d', '-h', '-lh'},
    'cat': {'-n', '-b', '-s'},
    'head': set(), 'tail': set(), 'wc': {'-l', '-w', '-c', '-m'},
    'rg': {'--files', '-n', '--line-number', '-l', '--files-with-matches', '-i',
           '--ignore-case', '-F', '--fixed-strings', '-S', '--smart-case', '-c',
           '--count', '-v', '--invert-match', '--hidden', '--no-heading'},
    'git': {'--short', '--porcelain', '--stat', '--name-only', '--name-status',
            '--oneline', '--all', '--cached', '--staged', '--no-renames'},
}
_VALUES = {'rg': {'-g', '--glob', '-t', '--type', '-A', '-B', '-C', '-m', '--max-count'},
           'head': {'-n', '-c'}, 'tail': {'-n', '-c'}, 'git': {'-n', '--max-count'}}


def compile_readonly(command: str, cwd: Path, resolver: PathResolver) -> ReadonlyCommand:
    """不能证明只读时抛 UnsupportedCommand，调用者决定拒绝或走一般审核。"""
    try:
        roots = bashlex.parse(command)
    except (ValueError, NotImplementedError) as exc:
        raise UnsupportedCommand(f'不支持的命令语法：{exc}') from exc
    if len(roots) != 1:
        raise UnsupportedCommand('只支持单条命令、管道与 &&')
    groups: list[list[list[str]]] = [[]]
    paths: list[Path] = [cwd]

    def path(value: str) -> str:
        if value == '-':
            return value
        if any(c in value for c in '*?[]~'):
            raise UnsupportedCommand('文件参数不展开通配符；请使用 rg -g')
        target = resolver.resolve(cwd / value)
        resolver.validate_local_read(target)
        paths.append(target)
        return str(target)

    def argv(node, piped=False) -> list[str]:
        if node.kind != 'command' or not node.parts:
            raise UnsupportedCommand('仅支持普通命令')
        if any(p.kind != 'word' or getattr(p, 'parts', []) for p in node.parts):
            raise UnsupportedCommand('不支持重定向、赋值、变量或命令替换')
        words = [p.word for p in node.parts]
        name, *args = words
        if name not in {'pwd', 'sed', *_FLAGS}:
            raise UnsupportedCommand(f'未登记的只读命令：{name}')
        executable = _resolve_rg() if name == 'rg' else shutil.which(name, path='/usr/bin:/bin:/usr/local/bin' if __import__('os').name == 'posix' else None)
        if not executable:
            raise UnsupportedCommand(f'未安装 {name}；可用 read_file 读取文件')
        result = [str(Path(executable).resolve())]
        if name == 'pwd':
            if args:
                raise UnsupportedCommand('pwd 不接受参数')
            return result
        if name == 'sed':
            if len(args) < 2 or args[0] != '-n' or not re.fullmatch(r'\d+(?:,\d+|,\$)?p', args[1]):
                raise UnsupportedCommand('sed 仅支持 -n 起始行[,结束行]p')
            return result + args[:2] + [path(v) for v in args[2:]]
        if name == 'git':
            if not args or args[0] not in {'status', 'diff', 'log', 'show', 'ls-files'}:
                raise UnsupportedCommand('git 仅支持 status/diff/log/show/ls-files')
            subcommand = args.pop(0)
            result += ['--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                       '-c', 'core.hooksPath=', subcommand]
            if subcommand in {'diff', 'log', 'show'}:
                result += ['--no-ext-diff', '--no-textconv']
        positional: list[str] = []
        revisions: list[str] = []
        i = 0
        literal = False
        while i < len(args):
            value = args[i]
            if value == '--' and not literal:
                literal = True
            elif value.startswith('-') and value != '-' and not literal:
                if value in _FLAGS[name]:
                    result.append(value)
                elif value in _VALUES.get(name, {}):
                    i += 1
                    if i >= len(args):
                        raise UnsupportedCommand(f'{value} 缺少参数')
                    if value not in {'-g', '--glob', '-t', '--type'} and not re.fullmatch(r'\d+', args[i]):
                        raise UnsupportedCommand(f'{value} 需要非负整数')
                    result += [value, args[i]]
                elif name in {'head', 'tail', 'git'} and re.fullmatch(r'-\d+', value):
                    result.append(value)
                else:
                    raise UnsupportedCommand(f'未登记的 {name} 选项：{value}')
            else:
                if name == 'git' and not literal:
                    if not re.fullmatch(r'[A-Za-z0-9_./~^:@{}-]+', value):
                        raise UnsupportedCommand('不支持的 git revision；路径请放在 -- 后')
                    revisions.append(value)
                elif name == 'rg' and '--files' not in result and not positional:
                    positional.append(value)
                else:
                    positional.append(path(value))
            i += 1
        if name == 'rg':
            result += ['--no-config', '--color=never']
            if '--files' not in result and not positional:
                raise UnsupportedCommand('rg 缺少搜索表达式')
            if '--files' not in result and len(positional) == 1 and not piped:
                positional.append(path('.'))
        result += revisions + ['--'] + positional
        return result

    def visit(node):
        if node.kind == 'list':
            for part in node.parts:
                if part.kind == 'operator':
                    if part.op != '&&':
                        raise UnsupportedCommand('只支持 &&，不支持后台执行或其他连接符')
                    groups.append([])
                else:
                    visit(part)
        elif node.kind == 'pipeline':
            commands = []
            for part in node.parts:
                if part.kind == 'pipe' and part.pipe == '|':
                    continue
                commands.append(argv(part, piped=bool(commands)))
            groups[-1].extend(commands)
        else:
            groups[-1].append(argv(node))

    visit(roots[0])
    if not all(groups):
        raise UnsupportedCommand('空命令')
    return ReadonlyCommand(tuple(tuple(tuple(c) for c in group) for group in groups), tuple(dict.fromkeys(paths)))
