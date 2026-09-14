"""将一次调用的授权编译成操作系统限制；不解释或改写 Shell 命令。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import stat
import subprocess
import tempfile


class SandboxError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionPolicy:
    command: str
    cwd: Path
    workspace: Path
    writable_roots: tuple[Path, ...] = ()
    network: bool = False
    root_identities: tuple[tuple[Path, int, int], ...] = field(init=False, repr=False)

    def __post_init__(self):
        identities = []
        for root in dict.fromkeys((self.workspace, *self.writable_roots)):
            info = root.stat()
            identities.append((root, info.st_dev, info.st_ino))
        object.__setattr__(self, 'root_identities', tuple(identities))

    @property
    def writes_workspace(self) -> bool:
        return any(self.workspace.is_relative_to(p) or p.is_relative_to(self.workspace)
                   for p in self.writable_roots)

    def validate(self, command: str, cwd: Path) -> None:
        if command != self.command or cwd != self.cwd or cwd.resolve() != cwd:
            raise SandboxError('permission_denied', '命令或目录与授权不一致')
        for root in (self.workspace, *self.writable_roots):
            if root.resolve() != root or not root.is_dir():
                raise SandboxError('permission_denied', f'授权目录已变化或不存在：{root}')
        for root, device, inode in self.root_identities:
            info = root.stat()
            if (info.st_dev, info.st_ino) != (device, inode):
                raise SandboxError('permission_denied', f'授权目录已被替换：{root}')


@dataclass(frozen=True)
class SandboxLaunch:
    argv: tuple[str, ...]
    environment: dict[str, str]
    pass_fds: tuple[int, ...] = ()


PROTECTED_NAMES = ('.git', '.agent', '.vscode', '.idea')


def protected_paths(policy: ExecutionPolicy) -> tuple[Path, ...]:
    # 保护每个可写根中的控制目录以及父级控制目录；阻止用宽泛扩权覆盖。
    roots = set(policy.writable_roots)
    paths = {Path.home() / '.agent', Path.home() / '.codex', Path.home() / '.ssh',
             Path.home() / '.aws', Path.home() / '.kube'}
    for root in roots:
        paths.update(root / name for name in PROTECTED_NAMES)
        from src.mgr.path_resolver import PathResolver
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs[:]:
                candidate = Path(directory) / name
                if PathResolver._is_protected_relative(candidate.relative_to(root)):
                    paths.add(candidate)
                    paths.add(candidate.resolve())
                    dirs.remove(name)
            for name in files:
                candidate = Path(directory) / name
                if PathResolver._is_protected_relative(candidate.relative_to(root)) or name == 'trusted_projects.json':
                    paths.add(candidate)
                    paths.add(candidate.resolve())
        # Git worktree 的 gitdir 可能位于工作区外，解析实际元数据目标。
        marker = root / '.git'
        if marker.is_file():
            first = marker.read_text(errors='replace').splitlines()
            if first and first[0].startswith('gitdir: '):
                paths.add((root / first[0][8:]).resolve())
    return tuple(sorted(paths))


def _seccomp_file(network: bool):
    """导出 BPF 给 bwrap；绝不把 seccomp 加载进 Agent 自身进程。"""
    # Linux 稳定 ABI；不调用 find_library 的外部探测进程。
    library = 'libseccomp.so.2'
    try:
        lib = ctypes.CDLL(library, use_errno=True)
    except OSError as exc:
        raise SandboxError('sandbox_unavailable', 'Linux Shell 需要 libseccomp.so.2') from exc
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    class Compare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                    ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                          ctypes.c_uint, ctypes.POINTER(Compare)]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxError('sandbox_setup_failed', '无法创建 seccomp 策略')
    output = tempfile.TemporaryFile()
    try:
        def deny(name, comparison=None):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                return
            result = lib.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, number,
                                                int(comparison is not None),
                                                ctypes.byref(comparison) if comparison is not None else None)
            if result < 0:
                raise SandboxError('sandbox_setup_failed', f'无法限制系统调用：{name}')
        for name in ('ptrace', 'process_vm_writev', 'process_vm_readv', 'mount', 'umount2',
                     'pivot_root', 'setns', 'unshare', 'bpf', 'keyctl', 'add_key', 'request_key',
                     'kexec_load', 'reboot', 'open_by_handle_at', 'init_module', 'finit_module',
                     'delete_module', 'userfaultfd', 'io_uring_setup'):
            deny(name)
        # 允许匿名 socketpair；拒绝宿主 Unix socket，网络授权也不能绕过它。
        if network:
            for family in (socket.AF_UNIX, socket.AF_NETLINK, socket.AF_PACKET, 40):
                deny('socket', Compare(0, 4, int(family), 0))  # SCMP_CMP_EQ
        else:
            deny('socket')
        deny('socketcall')  # 兼容 ABI 不得绕过 socket 过滤
        if lib.seccomp_export_bpf(context, output.fileno()) < 0:
            raise SandboxError('sandbox_setup_failed', '无法导出 seccomp 策略')
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        lib.seccomp_release(context)


class SandboxBackend:
    def __init__(self, shell: str | None = None, bubblewrap: str | None = None):
        self.system = platform.system()
        if not shell and os.name == 'posix':
            import pwd
            shell = pwd.getpwuid(os.getuid()).pw_shell
        self.shell = shell or '/bin/sh'
        self.bubblewrap = bubblewrap

    @contextmanager
    def prepare(self, policy: ExecutionPolicy, environment: dict[str, str]):
        policy.validate(policy.command, policy.cwd)
        if self.system not in {'Darwin', 'Linux'}:
            raise SandboxError('sandbox_unavailable', 'Shell 沙箱仅支持 macOS、Linux')
        if not Path(self.shell).is_absolute() or not os.access(self.shell, os.X_OK):
            raise SandboxError('sandbox_unavailable', 'Shell 必须是可执行的绝对路径')
        with tempfile.TemporaryDirectory(prefix='agent-shell-') as directory:
            scratch = Path(directory).resolve()
            env = {k: v for k, v in environment.items()
                   if not k.startswith(('LD_', 'DYLD_')) and k not in {'BASH_ENV', 'ENV', 'ZDOTDIR', 'SHELLOPTS', 'BASHOPTS'}}
            env.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch),
                       XDG_CACHE_HOME=str(scratch / 'cache'), UV_CACHE_DIR=str(scratch / 'uv'),
                       PYTHONPYCACHEPREFIX=str(scratch / 'pycache'), TMPPREFIX=str(scratch / 'zsh'), GIT_OPTIONAL_LOCKS='0',
                       GIT_PAGER='cat', PAGER='cat')
            # 只把随包 rg 放进专用 bin，不把整个冻结资源目录放进 PATH。
            from src.mgr.ripgrep import resolve_rg
            rg = resolve_rg()
            if rg:
                bin_dir = scratch / 'bin'
                bin_dir.mkdir()
                (bin_dir / 'rg').symlink_to(rg)
                env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', os.defpath)
            shell = [self.shell, '-c', policy.command]
            # zsh 即使非交互也读取 .zshenv；-f 禁止用户启动脚本。
            if Path(self.shell).name == 'zsh':
                shell.insert(1, '-f')
            protected = protected_paths(policy)
            if self.system == 'Darwin':
                executable = '/usr/bin/sandbox-exec'
                if not os.access(executable, os.X_OK):
                    raise SandboxError('sandbox_unavailable', '缺少 /usr/bin/sandbox-exec')
                quote = lambda p: json.dumps(str(p), ensure_ascii=False)
                profile = ['(version 1)', '(deny default)', '(allow file-read*)',
                           '(allow process-exec process-fork)', '(allow signal (target same-sandbox))',
                           '(allow process-info* (target same-sandbox))', '(allow sysctl-read)',
                           '(allow file-write-data (literal "/dev/null"))']
                for root in (*policy.writable_roots, scratch):
                    profile.append(f'(allow file-write* (subpath {quote(root)}))')
                for path in protected:
                    profile.append(f'(deny file-write* (subpath {quote(path)}))')
                if policy.network:
                    profile.append('(allow network-outbound network-inbound (remote ip))')
                    # DNS 和 TLS 由系统服务完成；只允许已知网络辅助服务。
                    services = ('com.apple.SystemConfiguration.DNSConfiguration',
                                'com.apple.SystemConfiguration.configd', 'com.apple.networkd',
                                'com.apple.SecurityServer', 'com.apple.ocspd', 'com.apple.trustd.agent')
                    profile.append('(allow mach-lookup ' + ' '.join(
                        f'(global-name {json.dumps(name)})' for name in services) + ')')
                profile_path = scratch / 'profile.sb'
                profile_path.write_text('\n'.join(profile))
                launch = SandboxLaunch(tuple([executable, '-f', str(profile_path), *shell]), env)
                self._probe(launch, policy.cwd)
                yield launch
            else:
                executable = self.bubblewrap or shutil.which('bwrap')
                if not executable or not os.path.isabs(executable) or not os.access(executable, os.X_OK):
                    raise SandboxError('sandbox_unavailable', 'Linux Shell 需要安装 bubblewrap')
                with _seccomp_file(policy.network) as filter_file, ExitStack() as descriptors:
                    argv = [executable, '--die-with-parent', '--new-session', '--unshare-all', '--unshare-user',
                            '--disable-userns', '--assert-userns-disabled', '--cap-drop', 'ALL', '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev']
                    if policy.network:
                        argv += ['--share-net']
                    pass_fds = [filter_file.fileno()]
                    def source(path):
                        # 固定 inode，防止授权目录被符号链接替换后扩大挂载权限。
                        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
                        descriptors.callback(os.close, fd)
                        info = os.fstat(fd)
                        expected = next(((d, i) for p, d, i in policy.root_identities if p == path), None)
                        if stat.S_ISLNK(info.st_mode) or path.resolve() != path or (expected is not None and expected != (info.st_dev, info.st_ino)):
                            raise SandboxError('permission_denied', '挂载源已变化')
                        pass_fds.append(fd)
                        return str(fd)
                    for root in (*policy.writable_roots, scratch):
                        argv += ['--bind-fd', source(root), str(root)]
                    for path in protected:
                        if path.exists():
                            argv += ['--ro-bind-fd', source(path.resolve()), str(path.resolve())]
                    argv += ['--seccomp', str(filter_file.fileno()), '--chdir', str(policy.cwd), '--', *shell]
                    launch = SandboxLaunch(tuple(argv), env, tuple(pass_fds))
                    self._probe(launch, policy.cwd)
                    yield launch

    @staticmethod
    def _probe(launch: SandboxLaunch, cwd: Path) -> None:
        # 独立探测沙箱安装是否成功，不根据用户程序 stderr 猜测框架错误。
        try:
            result = subprocess.run((*launch.argv[:-1], 'exit 0'), cwd=cwd,
                                    env=launch.environment, pass_fds=launch.pass_fds,
                                    capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError('sandbox_setup_failed', f'沙箱探测失败：{exc}') from exc
        finally:
            for fd in launch.pass_fds:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass  # O_PATH 目录描述符没有读取位置
        if result.returncode:
            raise SandboxError('sandbox_setup_failed', result.stderr.decode(errors='replace')[:2048])
