"""SQLite connection helpers and schema migrations (PRAGMA user_version)."""

import sqlite3
from pathlib import Path

# Each entry is one migration; never edit an applied one, append a new one instead.
MIGRATIONS: list[str] = [
    # 1: catalog and character registry
    """
    CREATE TABLE videos (
        video_id     TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        duration_s   INTEGER,
        upload_date  TEXT,              -- YYYY-MM-DD when known
        playlists    TEXT NOT NULL DEFAULT '[]',  -- JSON list of playlist titles
        kind         TEXT,              -- episode | non_episode | unknown
        kind_reason  TEXT,
        raw          TEXT NOT NULL,     -- JSON from yt-dlp
        fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE episodes (
        id           INTEGER PRIMARY KEY,   -- running episode number
        video_id     TEXT UNIQUE REFERENCES videos(video_id),
        season       INTEGER,
        title        TEXT NOT NULL,
        air_date     TEXT,
        duration_s   INTEGER
    );

    CREATE TABLE episode_steps (
        episode_id   INTEGER NOT NULL REFERENCES episodes(id),
        step         TEXT NOT NULL,     -- download | prepare | transcribe | ...
        status       TEXT NOT NULL,     -- pending | running | done | failed
        error        TEXT,
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (episode_id, step)
    );

    CREATE TABLE characters (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL,     -- glavni | na_ekranu | samo_u_prici | samo_glas
        played_by    TEXT REFERENCES characters(id),
        description  TEXT,
        source       TEXT NOT NULL DEFAULT 'seed'
    );

    CREATE TABLE aliases (
        character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
        alias        TEXT NOT NULL,
        alias_fold   TEXT NOT NULL,
        PRIMARY KEY (character_id, alias)
    );
    CREATE INDEX aliases_fold ON aliases(alias_fold);
    """,
    # 2: specials are processed like episodes; they get ids from SPECIAL_ID_BASE + 1
    """
    ALTER TABLE episodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'episode';  -- episode | special
    """,
    # 3: episode rows are never deleted, only retired; aliases record where they came from
    """
    -- 1 = no longer produced by the classifier. video_id is cleared (UNIQUE) and the
    -- former one kept in retired_video_id, so ids are never reused.
    ALTER TABLE episodes ADD COLUMN retired INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE episodes ADD COLUMN retired_video_id TEXT;
    -- seed | pipeline | owner; seed reloads only replace 'seed' aliases.
    ALTER TABLE aliases ADD COLUMN source TEXT NOT NULL DEFAULT 'seed';
    """,
    # 4: videos that vanished from the channel
    """
    -- 1 = not present in the latest complete catalog fetch (deleted or made private).
    ALTER TABLE videos ADD COLUMN missing INTEGER NOT NULL DEFAULT 0;
    """,
    # 5: phase 1 – pilot set, step bookkeeping, transcript segments
    """
    CREATE TABLE pilot (
        episode_id   INTEGER PRIMARY KEY REFERENCES episodes(id),
        grp          TEXT NOT NULL,   -- owner_guests | owner_remembered | first_50 | spread
        reason       TEXT
    );
    -- A step result is valid only for the video it was produced from.
    ALTER TABLE episode_steps ADD COLUMN video_id TEXT;
    ALTER TABLE episode_steps ADD COLUMN started_at TEXT;
    ALTER TABLE episode_steps ADD COLUMN elapsed_s REAL;
    ALTER TABLE episode_steps ADD COLUMN info TEXT;  -- JSON metrics of the run

    CREATE TABLE segments (
        episode_id   INTEGER NOT NULL REFERENCES episodes(id),
        idx          INTEGER NOT NULL,
        start_ms     INTEGER NOT NULL,
        end_ms       INTEGER NOT NULL,
        text         TEXT NOT NULL,   -- as returned by Whisper
        text_latin   TEXT NOT NULL,
        text_fold    TEXT NOT NULL,   -- Latin, no diacritics, lowercase (search form)
        actor        TEXT,            -- cvarkov | torbica | boskic | other (voice)
        character_id TEXT REFERENCES characters(id),
        PRIMARY KEY (episode_id, idx)
    );
    """,
    # 6: LLM description of an episode, one row per model and prompt version
    """
    CREATE TABLE enrichment (
        episode_id     INTEGER NOT NULL REFERENCES episodes(id),
        model          TEXT NOT NULL,     -- config name: qwen | glm | ...
        prompt_version INTEGER NOT NULL,
        status         TEXT NOT NULL,     -- done | failed
        data           TEXT,              -- JSON per dp/enrich/prompt.py schema
        raw            TEXT,              -- model output as returned
        error          TEXT,
        tokens_in      INTEGER,
        tokens_out     INTEGER,
        elapsed_s      REAL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (episode_id, model, prompt_version)
    );
    """,
    # 7: voice profiles of the main actors and the per-segment match score
    """
    CREATE TABLE voice_profiles (
        actor       TEXT PRIMARY KEY,   -- character id of the actor's main role
        embedding   BLOB NOT NULL,      -- float32 ECAPA vector, L2-normalized
        windows     INTEGER NOT NULL,
        seconds     REAL NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    ALTER TABLE segments ADD COLUMN actor_score REAL;  -- cosine to the chosen profile
    """,
    # 8: search index – transcript windows and description pieces, vectors + FTS5
    """
    CREATE TABLE windows (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        kind        TEXT NOT NULL,     -- transcript | summary | story | imitation
        start_ms    INTEGER,           -- NULL for the whole-episode summary
        end_ms      INTEGER,
        text        TEXT NOT NULL,
        embedding   BLOB NOT NULL      -- float32 bge-m3 vector, L2-normalized
    );
    CREATE INDEX windows_episode ON windows(episode_id);
    -- rowid = windows.id; text is folded (Latin, no diacritics, lowercase).
    CREATE VIRTUAL TABLE windows_fts USING fts5(text_fold, tokenize='trigram');
    """,
    # 9: a description belongs to the video it was made from. Existing rows (the pilot) are
    # backfilled with the episode's video (the retired one for a retired episode), so a
    # later re-point makes them stale.
    """
    ALTER TABLE enrichment ADD COLUMN video_id TEXT;
    UPDATE enrichment SET video_id =
        (SELECT coalesce(video_id, retired_video_id) FROM episodes
         WHERE id = enrichment.episode_id);
    """,
    # 10: window ids must never be reused (search keeps ids in memory and in FTS rowids);
    # rebuild with AUTOINCREMENT, ids copied unchanged so windows_fts rowids stay valid
    """
    CREATE TABLE windows_new (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        kind        TEXT NOT NULL,
        start_ms    INTEGER,
        end_ms      INTEGER,
        text        TEXT NOT NULL,
        embedding   BLOB NOT NULL
    );
    INSERT INTO windows_new (id, episode_id, kind, start_ms, end_ms, text, embedding)
        SELECT id, episode_id, kind, start_ms, end_ms, text, embedding FROM windows;
    DROP TABLE windows;
    ALTER TABLE windows_new RENAME TO windows;
    CREATE INDEX windows_episode ON windows(episode_id);
    """,
    # 11: phase 2 worker – retry bookkeeping per step, and the machine's health log
    """
    ALTER TABLE episode_steps ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE episode_steps ADD COLUMN retry_after TEXT;  -- UTC; failed step waits until then
    CREATE TABLE worker_health (
        at          TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC
        cpu_c       REAL,
        gpu_c       REAL,
        nvme_c      REAL,
        state       TEXT NOT NULL,     -- working | hot | paused | idle
        episode_id  INTEGER
    );
    CREATE INDEX worker_health_at ON worker_health(at);
    """,
    # 12: descriptions made through the Message Batches API (results come back up to a day
    # later, so what was sent is kept here and collected by `dp enrich-batch collect`)
    """
    CREATE TABLE enrich_batches (
        id              TEXT PRIMARY KEY,                     -- Anthropic batch id
        model           TEXT NOT NULL,                        -- config name
        prompt_version  INTEGER NOT NULL,
        episodes        TEXT NOT NULL,                        -- JSON {episode_id: video_id}
        submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC
        status          TEXT NOT NULL DEFAULT 'submitted',    -- submitted | collected
        collected_at    TEXT,
        summary         TEXT                                  -- JSON counts after collect
    );
    """,
]


def active_video_id(conn: sqlite3.Connection, episode_id: int) -> str | None:
    """Video of an episode that exists, is not retired and has a video; else None. The one
    definition of an active episode (search.py and eval repeat it in SQL)."""
    row = conn.execute(
        "SELECT video_id FROM episodes WHERE id=? AND retired=0", (episode_id,)
    ).fetchone()
    return row[0] if row else None


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into single statements (comments stay attached)."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf)
            buf = ""
    # Trailing text without a terminating ';' is only kept if it is not just comments.
    if any(ln.strip() and not ln.strip().startswith("--") for ln in buf.splitlines()):
        statements.append(buf)
    return statements


def _check_version(version: int) -> None:
    if version > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema v{version} is newer than this code (v{len(MIGRATIONS)})"
        )


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting schema version.

    Safe against concurrent migrators: each migration runs in its own
    BEGIN IMMEDIATE transaction and re-reads user_version under that write lock,
    so a migration another connection already applied is skipped. Statements are
    run one by one with execute() in manual transaction mode because
    executescript() would COMMIT the open transaction first.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    _check_version(version)
    if version == len(MIGRATIONS):
        return version

    saved_isolation = conn.isolation_level
    conn.isolation_level = None  # no implicit BEGIN/COMMIT from the sqlite3 module
    try:
        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                _check_version(version)
                if version == len(MIGRATIONS):
                    conn.execute("ROLLBACK")
                    break
                for stmt in _split_statements(MIGRATIONS[version]):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={version + 1}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.isolation_level = saved_isolation
    return len(MIGRATIONS)
