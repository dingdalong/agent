"""将可证明只读的 Shell AST 编译为 argv；授权与执行共用同一份命令计划。"""
from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import re
import shutil

import bashlex

from src.mgr.file_mgr import _resolve_rg
from src.mgr.path_resolver import PathResolver


@dataclass(frozen=True)
class CommandStage:
    argv: tuple[str, ...]
    redirects: tuple[tuple[int, int | str], ...] = ()


@dataclass(frozen=True)
class CommandPipeline:
    stages: tuple[CommandStage, ...]
    conditional: bool = False


@dataclass(frozen=True)
class ReadonlyCommand:
    pipelines: tuple[CommandPipeline, ...]
    paths: tuple[Path, ...]
    cwd: Path


class UnsupportedCommand(ValueError):
    def __init__(self, message: str, position: int | None = None, *, kind: str = "policy_unsupported"):
        super().__init__(message)
        self.position = position
        self.kind = kind


# 选项白名单排除外部程序、配置注入和文件写入。
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


def _word(raw: str) -> tuple[str, str]:
    """保留双引号内正则反斜杠，并仅展开未引用的路径通配符。"""
    value, pattern = [], []
    quote = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch in "\"'" and quote != ("'" if ch == '"' else '"'):
            quote = None if quote == ch else ch
            i += 1
            continue
        if ch == '\\' and quote != "'" and i + 1 < len(raw):
            following = raw[i + 1]
            if quote is None or following in '$`"\\\n':
                if following != '\n':
                    value.append(following)
                    pattern.append(glob.escape(following))
                i += 2
                continue
        if quote is None and ch in '{}~':
            raise UnsupportedCommand('不支持花括号或 ~ 展开')
        value.append(ch)
        pattern.append(glob.escape(ch) if quote else ch)
        i += 1
    return ''.join(value), ''.join(pattern)


def compile_readonly(command: str, cwd: Path, resolver: PathResolver) -> ReadonlyCommand:
    try:
        roots = bashlex.parse(command)
    except (ValueError, NotImplementedError) as exc:
        raise UnsupportedCommand(f'无法解析 Shell 语法：{exc}', kind='invalid_syntax') from exc
    paths: list[Path] = [cwd]
    entries: list[tuple[object, str | None]] = []
    connector = None

    def flatten(node):
        nonlocal connector
        if node.kind == 'list':
            for part in node.parts:
                if part.kind == 'operator':
                    if part.op not in {'&&', ';', '\n'}:
                        raise UnsupportedCommand('只支持 &&、分号和换行', part.pos[0])
                    connector = part.op
                else:
                    flatten(part)
        elif node.kind in {'command', 'pipeline'}:
            entries.append((node, connector))
            connector = None
        elif node.kind == 'function':
            raise UnsupportedCommand('只读策略不支持 Shell 函数定义', node.pos[0])
        else:
            raise UnsupportedCommand('只支持普通命令和安全管道', node.pos[0])

    for root in roots:
        flatten(root)
    if not entries:
        raise UnsupportedCommand('命令不能为空')

    def words(node):
        result = []
        for part in node.parts:
            if part.kind == 'redirect':
                continue
            if part.kind != 'word' or getattr(part, 'parts', []):
                raise UnsupportedCommand('不支持赋值、变量或命令替换', part.pos[0])
            result.append(_word(command[slice(*part.pos)]))
        return result

    # 只支持开头 cd；后续所有路径在同一经过验证的工作目录下解析。
    first, _ = entries[0]
    if first.kind == 'command' and words(first)[:1] == [('cd', 'cd')]:
        args = words(first)
        if len(args) != 2 or any(p.kind == 'redirect' for p in first.parts) or len(entries) < 2 or entries[1][1] != '&&':
            raise UnsupportedCommand('仅支持开头 cd <目录> &&')
        if glob.has_magic(args[1][1]) and args[1][1] != glob.escape(args[1][0]):
            raise UnsupportedCommand('cd 需要明确目录')
        cwd = resolver.resolve(cwd / args[1][0])
        resolver.validate_local_read(cwd)
        if not cwd.is_dir():
            raise UnsupportedCommand('cd 目标不是目录')
        paths.append(cwd)
        entries.pop(0)

    def path(word, base=None):
        base = cwd if base is None else base
        value, pattern = word
        if value == '-':
            return ['-']
        # 展开前校验确定的目录前缀，展开后再逐一校验实际目标。
        if glob.has_magic(pattern):
            prefix = []
            for part in Path(pattern).parts:
                if glob.has_magic(part):
                    break
                prefix.append(part)
            resolver.validate_local_read(resolver.resolve(base / Path(*prefix)))
            matches = sorted(glob.glob(str(base / pattern)))
        else:
            matches = []
        targets = [resolver.resolve(p) for p in matches] or [resolver.resolve(base / value)]
        for target in targets:
            resolver.validate_local_read(target)
            paths.append(target)
        return [str(p) for p in targets]

    def stage(node, piped=False):
        if node.kind != 'command':
            raise UnsupportedCommand('管道仅支持普通命令', node.pos[0])
        parsed = words(node)
        if not parsed:
            raise UnsupportedCommand('缺少命令', node.pos[0])
        name, _ = parsed[0]
        args = parsed[1:]
        if name not in {'pwd', 'sed', 'echo', *_FLAGS}:
            raise UnsupportedCommand(f'未登记的只读命令：{name}', node.pos[0])
        redirects = []
        for part in node.parts:
            if part.kind != 'redirect':
                continue
            source = part.input if part.input is not None else 1
            target = part.output
            if part.type == '>' and source == 2 and getattr(target, 'kind', None) == 'word' and not getattr(target, 'parts', []) and _word(command[slice(*target.pos)])[0] == '/dev/null':
                redirects.append((2, 'null'))
            elif part.type == '>&' and (source, target) in {(1, 2), (2, 1)}:
                redirects.append((source, target))
            else:
                raise UnsupportedCommand('只支持 2>/dev/null、2>&1、1>&2', part.pos[0])
        executable = _resolve_rg() if name == 'rg' else shutil.which(name, path='/usr/bin:/bin:/usr/local/bin' if os.name == 'posix' else None)
        if not executable:
            raise UnsupportedCommand(f'未安装 {name}')
        result = [str(Path(executable).resolve())]
        if name == 'echo':
            if any(re.fullmatch(r'-[neE]+', value) or '\\' in value for value, _ in args):
                raise UnsupportedCommand('echo 仅支持普通文字分隔符，不支持选项或反斜杠')
            return CommandStage(tuple(result + [v for v, _ in args]), tuple(redirects))
        if name == 'pwd':
            if args:
                raise UnsupportedCommand('pwd 不接受参数')
            return CommandStage(tuple(result), tuple(redirects))
        if name == 'sed':
            if len(args) < 2 or args[0][0] != '-n' or not re.fullmatch(r'\d+(?:,\d+|,\$)?p', args[1][0]):
                raise UnsupportedCommand('sed 仅支持 -n 起始行[,结束行]p')
            return CommandStage(tuple(result + [v for v, _ in args[:2]] + [p for word in args[2:] for p in path(word)]), tuple(redirects))
        git_cwd = cwd
        if name == 'git':
            while args and args[0][0] == '-C':
                args.pop(0)
                if not args:
                    raise UnsupportedCommand('git -C 缺少目录')
                directory, pattern = args.pop(0)
                if glob.has_magic(pattern) and pattern != glob.escape(directory):
                    raise UnsupportedCommand('git -C 需要明确目录，不展开通配符')
                # Git -C "" 不改变工作目录；相对目录基于上一个 -C。
                if directory:
                    git_cwd = resolver.resolve(git_cwd / directory)
                    resolver.validate_local_read(git_cwd)
                    if not git_cwd.is_dir():
                        raise UnsupportedCommand('git -C 目标不是目录')
                    paths.append(git_cwd)
            result += ['-C', str(git_cwd)]
            if not args or args[0][0] not in {'status', 'diff', 'log', 'show', 'ls-files'}:
                raise UnsupportedCommand('git 仅支持 status/diff/log/show/ls-files')
            subcommand = args.pop(0)[0]
            result += ['--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=', subcommand]
            if subcommand in {'diff', 'log', 'show'}:
                result += ['--no-ext-diff', '--no-textconv']
        positional, revisions = [], []
        i, literal = 0, False
        while i < len(args):
            value = args[i][0]
            if value == '--' and not literal:
                literal = True
            elif value.startswith('-') and value != '-' and not literal:
                if value in _FLAGS[name]:
                    result.append(value)
                elif value in _VALUES.get(name, {}):
                    i += 1
                    if i >= len(args):
                        raise UnsupportedCommand(f'{value} 缺少参数')
                    if value not in {'-g', '--glob', '-t', '--type'} and not re.fullmatch(r'\d+', args[i][0]):
                        raise UnsupportedCommand(f'{value} 需要非负整数')
                    result += [value, args[i][0]]
                elif name in {'head', 'tail', 'git'} and re.fullmatch(r'-\d+', value):
                    result.append(value)
                else:
                    raise UnsupportedCommand(f'只读策略尚不支持 {name} 选项：{value}')
            elif name == 'git' and not literal:
                if not re.fullmatch(r'[A-Za-z0-9_./~^:@{}-]+', value):
                    raise UnsupportedCommand('不支持的 git revision；路径请放在 -- 后')
                revisions.append(value)
            elif name == 'rg' and '--files' not in result and not positional:
                positional.append(value)
            else:
                positional.extend(path(args[i], git_cwd if name == 'git' else cwd))
            i += 1
        if name == 'rg':
            result += ['--no-config', '--color=never']
            if '--files' not in result and not positional:
                raise UnsupportedCommand('rg 缺少搜索表达式')
            if '--files' not in result and len(positional) == 1 and not piped:
                positional.extend(path(('.', '.')))
        result += revisions + ['--'] + positional
        return CommandStage(tuple(result), tuple(redirects))

    pipelines = []
    for node, connection in entries:
        if node.kind == 'pipeline':
            stages = []
            for part in node.parts:
                if part.kind == 'pipe' and part.pipe == '|':
                    continue
                stages.append(stage(part, piped=bool(stages)))
        else:
            stages = [stage(node)]
        pipelines.append(CommandPipeline(tuple(stages), connection == '&&'))
    return ReadonlyCommand(tuple(pipelines), tuple(dict.fromkeys(paths)), cwd)
