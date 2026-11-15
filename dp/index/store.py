"""Writes on the windows table that need no model or numpy (the catalog layer uses them)."""

import sqlite3


def delete_windows(conn: sqlite3.Connection, episode_id: int) -> None:
    """Remove all windows of the episode with their FTS rows. The caller owns the
    transaction."""
    conn.execute(
        "DELETE FROM windows_fts WHERE rowid IN (SELECT id FROM windows WHERE episode_id=?)",
        (episode_id,),
    )
    conn.execute("DELETE FROM windows WHERE episode_id=?", (episode_id,))
