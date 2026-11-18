"""Supervised HTTP workers and exclusive ownership of startup recovery."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class RuntimeOwnership:
    """An OS-held lock; process exit releases ownership without stale PID files.

    The sidecar stays on disk: unlinking it could create two separately locked
    inodes. This is a local-filesystem, single-server contract, not a lease.
    """

    def __init__(self, database):
        self.path = None if str(database) == ":memory:" else Path(database).resolve()
        self.handle = None

    def __enter__(self):
        if self.path is None:
            return self
        handle = open(str(self.path) + ".runtime.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("数据库运行锁不可用；请确认没有其他服务或终端使用同一数据库") from exc
        self.handle = handle
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class RuntimeWorkers:
    """Keep transient iteration failures visible until that worker succeeds."""

    def __init__(self):
        self.stop = asyncio.Event()
        self.tasks = {}
        self.errors = {}
        self.wakes = []

    def start(self, name, operation, *, interval, wake=None):
        if wake is not None:
            self.wakes.append(wake)

        async def run():
            while not self.stop.is_set():
                if wake is not None:
                    wake.clear()
                try:
                    await operation()
                except Exception as exc:
                    # No exception text or traceback: upstream failures may
                    # contain credentials, signed URLs or private model data.
                    error = type(exc).__name__
                    if self.errors.get(name) != error:
                        logger.error("Runtime worker %s failed (%s)", name, error)
                    self.errors[name] = error
                else:
                    self.errors.pop(name, None)
                try:
                    await asyncio.wait_for((wake or self.stop).wait(), timeout=interval)
                except TimeoutError:
                    pass

        self.tasks[name] = asyncio.create_task(run(), name="cockp:" + name)

    @property
    def healthy(self):
        return not self.errors and all(not task.done() for task in self.tasks.values())

    async def close(self):
        self.stop.set()
        for wake in self.wakes:
            wake.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks.values(), timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
