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


def test_migrations_9_10_backfill_video_and_keep_window_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "t.sqlite")
    monkeypatch.setattr(dp.db, "MIGRATIONS", MIGRATIONS[:8])
    migrate(conn)
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v1', 't', '{}')")
    conn.execute("INSERT INTO episodes (id, video_id, title) VALUES (1, 'v1', 'E')")
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data) "
        "VALUES (1, 'm', 1, 'done', '{}')"
    )
    # A retired episode: video_id is NULL, its old video is in retired_video_id.
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v0', 't', '{}')")
    conn.execute(
        "INSERT INTO episodes (id, video_id, title, retired, retired_video_id) "
        "VALUES (2, NULL, 'R', 1, 'v0')"
    )
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data) "
        "VALUES (2, 'm', 1, 'done', '{}')"
    )
    for i in (1, 2, 5):
        conn.execute(
            "INSERT INTO windows (id, episode_id, kind, text, embedding) "
            "VALUES (?, 1, 'k', 't', x'00')",
            (i,),
        )
        conn.execute("INSERT INTO windows_fts (rowid, text_fold) VALUES (?, 'word')", (i,))
    conn.commit()
    monkeypatch.setattr(dp.db, "MIGRATIONS", MIGRATIONS)
    assert migrate(conn) == len(MIGRATIONS)
    # Backfilled: the pilot row belongs to the video the episode points to now.
    assert conn.execute("SELECT video_id FROM enrichment WHERE episode_id=1").fetchone()[0] == "v1"
    assert conn.execute("SELECT video_id FROM enrichment WHERE episode_id=2").fetchone()[0] == "v0"
    assert [r[0] for r in conn.execute("SELECT id FROM windows ORDER BY id")] == [1, 2, 5]
    assert (
        conn.execute("SELECT COUNT(*) FROM windows_fts WHERE rowid IN (1, 2, 5)").fetchone()[0] == 3
    )
    conn.execute("DELETE FROM windows WHERE id=5")
    cur = conn.execute(
        "INSERT INTO windows (episode_id, kind, text, embedding) VALUES (1, 'k', 't', x'00')"
    )
    assert cur.lastrowid == 6  # id 5 is not handed out again


def test_active_video_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v1', 't', '{}')")
    conn.execute("INSERT INTO episodes (id, video_id, title) VALUES (1, 'v1', 'A')")
    conn.execute("INSERT INTO episodes (id, video_id, title) VALUES (2, NULL, 'B')")
    conn.execute(
        "INSERT INTO episodes (id, video_id, title, retired, retired_video_id) "
        "VALUES (3, NULL, 'C', 1, 'v1')"
    )
    conn.execute("INSERT INTO episodes (id, video_id, title, retired) VALUES (4, NULL, 'D', 1)")
    assert dp.db.active_video_id(conn, 1) == "v1"
    assert [dp.db.active_video_id(conn, i) for i in (2, 3, 4, 99)] == [None] * 4
