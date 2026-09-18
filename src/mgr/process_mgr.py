"""会话内进程、输出位置与修改租约的唯一所有者。"""
from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import os
from pathlib import Path
import signal
import uuid

from src.mode import RunMode
from src.common.frozen import clean_env
from src.common.sandbox import ExecutionPolicy, SandboxBackend, SandboxError
from src.tools.display import ToolResult


class WorkspaceAccess:
    """同一工作区的读写租约，运行中的命令也必须持有到进程结束。"""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    async def acquire(self):
        async with self.condition:
            self.waiting_writers += 1
            try:
                await self.condition.wait_for(lambda: not self.writer and not self.readers)
                self.writer = True
            finally:
                self.waiting_writers -= 1
                self.condition.notify_all()

    async def release(self):
        async with self.condition:
            self.writer = False
            self.condition.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        await self.release()

    @asynccontextmanager
    async def read(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.writer and not self.waiting_writers)
            self.readers += 1
        try:
            yield
        finally:
            async with self.condition:
                self.readers -= 1
                self.condition.notify_all()


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
    def __init__(self, output_bytes: int = 1024 * 1024, sandbox: SandboxBackend | None = None):
        self.sandbox = sandbox or SandboxBackend()
        self.sessions: dict[str, ProcessSession] = {}
        self.output_bytes = output_bytes
        self.workspace_lock = WorkspaceAccess()

    async def start(self, owner, command, cwd, environment, policy: ExecutionPolicy, timeout_ms, yield_time_ms, stdin_open=False):
        if len(self.sessions) >= 32:
            return ToolResult.failure("process_limit", "并行进程达到 32 个，请先轮询或终止现有进程")
        session_id = uuid.uuid4().hex
        session = ProcessSession(owner, not policy.writes_workspace)
        self.sessions[session_id] = session
        session.task = asyncio.create_task(self._run(session, command, cwd, environment, policy, timeout_ms, stdin_open))
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

    async def _run(self, session, command, cwd, environment, policy, timeout_ms, stdin_open):
        lease = self.workspace_lock.read() if session.readonly else self.workspace_lock
        acquired = False
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                await lease.__aenter__()
                acquired = True
                await asyncio.to_thread(policy.validate, command, cwd)
                manager = self.sandbox.prepare(policy, clean_env(environment))
                prepare = asyncio.create_task(asyncio.to_thread(manager.__enter__))
                try:
                    launch = await asyncio.shield(prepare)
                except asyncio.CancelledError:
                    # 工作线程不能取消，等它释放临时目录和过滤器后再传播取消。
                    try:
                        await prepare
                    except Exception:
                        pass
                    else:
                        await asyncio.to_thread(manager.__exit__, None, None, None)
                    raise
                try:
                    await self._execute(session, launch, cwd, stdin_open)
                finally:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
        except SandboxError as exc:
            session.error_code = exc.code
            self._capture(session, str(exc).encode())
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
    
    async def _execute(self, session, launch, cwd, stdin_open):
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *launch.argv, cwd=str(cwd), env=launch.environment, pass_fds=launch.pass_fds,
            stdin=asyncio.subprocess.PIPE if stdin_open else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc = await spawn
            session.processes.append(proc)
            await self._terminate(session)
            raise
        session.processes.append(proc)
        reader = asyncio.create_task(self._drain(session, proc.stdout))
        try:
            session.exit_code = await proc.wait()
            await reader
        finally:
            await self._terminate(session)
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

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
        session.processes.clear()

    async def poll(self, owner, session_id, chars='', yield_time_ms=1000, terminate=False, mode=RunMode.EXECUTE):
        if terminate and chars:
            return ToolResult.failure('invalid_arguments', 'terminate 与非空 chars 不能同时使用', recovery='只传 terminate=true，或只传 chars。')
        session = self.sessions.get(session_id)
        if session is None or session.owner != owner:
            return ToolResult.failure('unknown_session', '进程不存在、已回收或不属于当前 agent')
        async with session.lock:
            if chars and mode is RunMode.PLAN and not session.readonly:
                return ToolResult.failure('permission_denied', '计划模式不能向有工作区写权限的旧进程发送输入')
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
            status = 'running' if running else ('cancelled' if session.error_code == 'cancelled' else 'error' if session.error_code else 'success')
            result = ToolResult(text, status=status, error_code=session.error_code, exit_code=None if running else session.exit_code, session_id=session_id if running else None, truncated=bool(session.dropped))
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
