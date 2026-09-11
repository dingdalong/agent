"""同一工作区的读写租约，运行中的命令也必须持有到进程结束。"""
import asyncio
from contextlib import asynccontextmanager


class WorkspaceAccess:
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
