"""会话内进程、输出位置与修改租约的唯一所有者。"""
from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass, field
import os
import signal
import uuid

from src.mgr.frozen import clean_env
from src.mgr.workspace_access import WorkspaceAccess
from src.tools.display import ToolResult


@dataclass
class ProcessSession:
    owner: tuple[str, str]
    readonly: bool
    processes: list = field(default_factory=list)
    output: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    exit_code: int | None = None
    error_code: str | None = None
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))


class ProcessMgr:
    def __init__(self, output_bytes: int = 1024 * 1024):
        self.sessions: dict[str, ProcessSession] = {}
        self.output_bytes = output_bytes
        self.workspace_lock = WorkspaceAccess()

    async def start(self, owner, command, cwd, environment, readonly, timeout_ms, yield_time_ms):
        if len(self.sessions) >= 32:
            return ToolResult.failure("process_limit", "并行进程达到 32 个，请先轮询或终止现有进程")
        session_id = uuid.uuid4().hex
        session = ProcessSession(owner, readonly is not None)
        self.sessions[session_id] = session
        session.task = asyncio.create_task(self._run(session, command, cwd, environment, readonly, timeout_ms))
        # 先让任务进入租约队列，避免 yield=0 时后续文件写入抢先执行。
        await asyncio.sleep(0)
        return await self.poll(owner, session_id, '', yield_time_ms, False)

    def _capture(self, session, chunk):
        combined = session.output + chunk
        if len(combined) > self.output_bytes:
            session.dropped += len(combined) - self.output_bytes
            head = self.output_bytes // 2
            combined = combined[:head] + combined[-(self.output_bytes - head):]
        session.output = bytearray(combined)

    async def _drain(self, session, stream):
        while chunk := await stream.read(65536):
            self._capture(session, chunk)

    async def _run(self, session, command, cwd, environment, readonly, timeout_ms):
        lease = self.workspace_lock.read() if session.readonly else self.workspace_lock
        acquired = False
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                await lease.__aenter__()
                acquired = True
                env = clean_env(environment)
                # 不继承影响只读命令解析、分页或 Git 外部执行的环境入口。
                if session.readonly:
                    env = {k: v for k, v in env.items() if not k.startswith(('GIT_', 'RIPGREP_', 'BASH_ENV', 'ENV', 'LD_', 'DYLD_'))}
                    env.update({'GIT_PAGER': 'cat', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
                groups = readonly.pipelines if readonly else [(None,)]
                for group in groups:
                    pending_fd = None
                    readers = []
                    current = []
                    try:
                        for index, argv in enumerate(group):
                            read_fd, write_fd = os.pipe() if index < len(group) - 1 else (None, None)
                            kwargs = dict(cwd=str(cwd), env=env, stdin=pending_fd if pending_fd is not None else asyncio.subprocess.PIPE,
                                          stdout=write_fd if write_fd is not None else asyncio.subprocess.PIPE,
                                          stderr=asyncio.subprocess.PIPE,
                                          **({'start_new_session': True} if os.name == 'posix' else {'creationflags': 0x00000200}))
                            try:
                                proc = await (asyncio.create_subprocess_exec(*argv, **kwargs) if argv else asyncio.create_subprocess_shell(command, **kwargs))
                            finally:
                                if pending_fd is not None:
                                    os.close(pending_fd)
                                if write_fd is not None:
                                    os.close(write_fd)
                                pending_fd = read_fd
                            session.processes.append(proc)
                            current.append(proc)
                            if session.readonly and proc.stdin:
                                proc.stdin.close()
                            if proc.stdout:
                                readers.append(asyncio.create_task(self._drain(session, proc.stdout)))
                            readers.append(asyncio.create_task(self._drain(session, proc.stderr)))
                        codes = await asyncio.gather(*(p.wait() for p in current))
                        await asyncio.gather(*readers)
                        # 保留管道任一阶段失败；rg 无匹配和下游提前关闭导致的 SIGPIPE 除外。
                        normalized = [0 if (readonly and argv and os.path.basename(argv[0]) in {'rg', 'rg.exe'} and code == 1) or code == -getattr(signal, 'SIGPIPE', 13) else code
                                      for argv, code in zip(group, codes)]
                        session.exit_code = next((code for code in normalized if code), 0)
                        if session.exit_code:
                            break
                    finally:
                        if pending_fd is not None:
                            os.close(pending_fd)
                        for reader in readers:
                            if not reader.done():
                                reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
        except TimeoutError:
            session.error_code = 'timeout'
        except asyncio.CancelledError:
            session.error_code = 'cancelled'
        except Exception as exc:
            session.error_code = 'execution_error'
            self._capture(session, str(exc).encode())
        finally:
            await self._terminate(session)
            if acquired:
                await lease.__aexit__(None, None, None)
    
    async def _terminate(self, session):
        for proc in session.processes:
            try:
                if os.name == 'posix':
                    os.killpg(proc.pid, signal.SIGKILL)
                elif proc.returncode is None:
                    killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F', env=clean_env(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def poll(self, owner, session_id, chars='', yield_time_ms=1000, terminate=False):
        session = self.sessions.get(session_id)
        if session is None or session.owner != owner:
            return ToolResult.failure('unknown_session', '进程不存在、已回收或不属于当前 agent')
        async with session.lock:
            if chars and session.readonly:
                return ToolResult.failure('permission_denied', '只读命令不接受 stdin')
            if terminate and session.task:
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            if chars:
                proc = session.processes[0] if session.processes else None
                if proc is None or proc.stdin is None or proc.returncode is not None:
                    return ToolResult.failure('stdin_closed', '进程尚未启动或输入已关闭')
                try:
                    proc.stdin.write(chars.encode())
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return ToolResult.failure('stdin_closed', '进程输入已关闭')
            if session.task and not session.task.done() and yield_time_ms:
                await asyncio.wait({session.task}, timeout=yield_time_ms / 1000)
            data = bytes(session.output)
            session.output.clear()
            running = bool(session.task and not session.task.done())
            text = session.decoder.decode(data, final=not running)
            if session.dropped:
                session.decoder.reset()
                head = self.output_bytes // 2
                text = data[:head].decode(errors='replace') + f'\n[中间丢失 {session.dropped} bytes；请缩小命令输出]\n' + data[head:].decode(errors='replace')
            running = bool(session.task and not session.task.done())
            status = 'running' if running else ('cancelled' if session.error_code == 'cancelled' else 'error' if session.error_code or session.exit_code else 'success')
            result = ToolResult(text, status=status, error_code=session.error_code or ('nonzero_exit' if not running and session.exit_code else None), exit_code=None if running else session.exit_code, session_id=session_id if running else None, truncated=bool(session.dropped))
            session.dropped = 0
            if not running:
                self.sessions.pop(session_id, None)
            return result

    async def close(self, owner=None):
        sessions = [(key, s) for key, s in self.sessions.items() if owner is None or s.owner == owner]
        for _, session in sessions:
            if session.task:
                session.task.cancel()
        await asyncio.gather(*(s.task for _, s in sessions if s.task), return_exceptions=True)
        for key, _ in sessions:
            self.sessions.pop(key, None)
