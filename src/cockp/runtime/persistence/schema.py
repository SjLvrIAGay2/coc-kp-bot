"""The packaged migration scripts are the authoritative database schema.

No service, integration or model module is imported to open a database.
"""
from __future__ import annotations

from importlib.resources import files
import sqlite3
import time
from collections.abc import Iterable


MIGRATION_FILES = (
    "001_initial.sql",
    "002_conversation.sql",
    "003_auxiliary.sql",
    "004_natural_actions.sql",
    "005_import_interactions.sql",
    "006_file_ingress.sql",
    "007_document_selection.sql",
    "008_official_transport.sql",
    "009_context_windows.sql",
    "010_snapshot_modules.sql",
)
MIGRATIONS = [
    (int(name.split("_", 1)[0]), files(__package__).joinpath("sql", name).read_text(encoding="utf-8"))
    for name in MIGRATION_FILES
]


def migration_sql(version: int) -> str:
    """Expose existing SQL constants from their single versioned source."""
    return dict(MIGRATIONS)[version]


def initialize_database(db: sqlite3.Connection, *, timeout: float = 30) -> None:
    """Configure the connection and upgrade before handing it to business code.

    SQLite's journal-mode switch may return SQLITE_BUSY immediately during
    simultaneous first startup, despite busy_timeout. Retry only lock errors,
    within the same bounded startup timeout; other failures remain visible.
    """
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    deadline = time.monotonic() + timeout
    while True:
        try:
            db.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                raise
            time.sleep(min(.01, max(0, deadline - time.monotonic())))
    apply_migrations(db)


def _statements(script: str):
    """Split using SQLite's parser, including quoted semicolons and triggers.

    executescript() implicitly commits a pending transaction. Execute complete
    statements individually so DDL and version markers share our writer lock.
    """
    start = 0
    for position, character in enumerate(script):
        if character == ";" and sqlite3.complete_statement(script[start:position + 1]):
            yield script[start:position + 1]
            start = position + 1
    if script[start:].strip():
        yield script[start:]


def apply_migrations(db: sqlite3.Connection, migrations: Iterable[tuple[int, str]] | None = None) -> None:
    """Upgrade atomically, rechecking versions after the cross-process lock.

    Safe for a fresh database, a legacy database, or simultaneous startup.
    A failure rolls back all DDL and markers from this upgrade attempt. The
    caller's transaction is never implicitly committed.
    """
    if db.in_transaction:
        raise sqlite3.ProgrammingError("数据库迁移不能在已有事务中执行")
    scripts = list(MIGRATIONS if migrations is None else migrations)
    versions = [version for version, _ in scripts]
    if versions != sorted(set(versions)) or any(version < 1 for version in versions):
        raise ValueError("数据库迁移版本必须为递增且唯一的正整数")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL)")
        applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        if applied.difference(versions):
            raise sqlite3.DatabaseError("数据库包含当前程序未知的迁移版本，请使用匹配版本的程序")
        for version, script in scripts:
            if version in applied:
                continue
            for statement in _statements(script):
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, time.time()))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
