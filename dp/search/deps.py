"""Read-only database access for the api service."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from dp.config import get_settings
from dp.db import MIGRATIONS, connect


class DatabaseNotReady(RuntimeError):
    """The database file is missing or its schema is not at this code's version."""


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    path = get_settings().db
    if not path.is_file():
        raise DatabaseNotReady(f"database not initialized ({path}); run dp.cli init")
    conn = connect(path, readonly=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            raise DatabaseNotReady(
                f"database not migrated (schema v{version}, code v{len(MIGRATIONS)}); "
                "run dp.cli init"
            )
        yield conn
    finally:
        conn.close()


def db_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    with open_db() as conn:
        yield conn
