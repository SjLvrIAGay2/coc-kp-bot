"""Run synchronous persistence work on one owned, context-bound connection.

Callbacks must finish synchronously and must not start event-loop tasks or do
network I/O. Cancellation waits for the database outcome before releasing its
caller; cancelling an await cannot undo a SQLite commit.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import inspect
from pathlib import Path
import sqlite3
import threading


class AsyncPersistence:
    def __init__(self, store):
        self.store = store
        self.path = store.path if store.path == ":memory:" else str(Path(store.path).resolve())
        self.executor = None
        self.connection = None
        self.lock = threading.RLock()
        self.closed = False

    def _invoke(self, operation, args, kwargs):
        if self.connection is None:
            self.connection = sqlite3.connect(self.path, check_same_thread=False,
                isolation_level=None, timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
        token = self.store._connection_binding.set((self.connection, self.lock))
        try:
            result = operation(*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Persistence callbacks must be synchronous")
            if isinstance(result, (sqlite3.Cursor, sqlite3.Connection)):
                raise TypeError("Persistence callbacks must return materialized values")
            if self.connection.in_transaction:
                raise RuntimeError("Persistence callback left an open transaction")
            return result
        finally:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            self.store._connection_binding.reset(token)

    async def run(self, operation, /, *args, **kwargs):
        if self.closed:
            raise RuntimeError("Persistence boundary is closed")
        if self.store.path == ":memory:":
            # Independent :memory: connections cannot share authoritative state.
            # Retain the synchronous test/operator contract; runtime databases
            # use a file and the dedicated connection below.
            return operation(*args, **kwargs)
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cockp-db")
        context = copy_context()
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, context.run, self._invoke, operation, args, kwargs)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    def close(self):
        self.closed = True
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        if self.connection is not None:
            self.connection.close()
            self.connection = None
