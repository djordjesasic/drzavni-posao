import sqlite3
from pathlib import Path

import pytest

import dp.db
from dp.db import MIGRATIONS, connect, migrate


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_migrate_refuses_newer_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    conn.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrate(conn)


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    assert migrate(conn) == migrate(conn) == len(MIGRATIONS)


def test_migrate_two_connections_same_file(tmp_path: Path) -> None:
    a = connect(tmp_path / "t.sqlite")
    b = connect(tmp_path / "t.sqlite")
    assert migrate(a) == len(MIGRATIONS)
    assert migrate(b) == len(MIGRATIONS)
    assert _columns(b, "videos").count("missing") == 1
    assert b.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migrate_skips_migrations_applied_after_stale_read(tmp_path: Path) -> None:
    """B reads the old version, then A migrates before B starts writing."""
    a = connect(tmp_path / "t.sqlite")
    b = connect(tmp_path / "t.sqlite")
    done: list[bool] = []

    def on_statement(sql: str) -> None:
        # Fires when B starts its first BEGIN, after B has read user_version = 0.
        if not done and sql.lstrip().upper().startswith("BEGIN"):
            done.append(True)
            migrate(a)

    b.set_trace_callback(on_statement)
    assert migrate(b) == len(MIGRATIONS)
    b.set_trace_callback(None)
    assert done, "A did not run in between"
    assert a.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert _columns(b, "videos").count("missing") == 1


def test_migrate_detects_newer_schema_on_reread(tmp_path: Path) -> None:
    a = connect(tmp_path / "t.sqlite")
    b = connect(tmp_path / "t.sqlite")

    def on_statement(sql: str) -> None:
        if sql.lstrip().upper().startswith("BEGIN"):
            b.set_trace_callback(None)
            a.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")

    b.set_trace_callback(on_statement)
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrate(b)
    assert not b.in_transaction
    assert b.isolation_level == ""


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    bad = """
    CREATE TABLE rollback_probe (x INTEGER);  -- must not survive
    INSERT INTO no_such_table VALUES (1);
    """
    monkeypatch.setattr(dp.db, "MIGRATIONS", [*MIGRATIONS, bad])
    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        migrate(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    probe = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'rollback_probe'").fetchone()
    assert probe is None
    # Connection is still usable and back in its original transaction mode.
    assert not conn.in_transaction
    assert conn.isolation_level == ""
    with conn:
        conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v1', 't', '{}')")
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 1
