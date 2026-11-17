"""Reads of stored enrichment results. Kept free of the LLM clients so the index step can
use it without importing them.

enrichment.status: done | failed | stale. 'stale' is a description of the current video
whose transcript changed afterwards (see mark_stale): `dp enrich` makes it again without
--force, and until then the index keeps using it, because an older description beats none.
"""

import sqlite3

from dp.enrich.prompt import PROMPT_VERSION


def valid_enrichment(
    conn: sqlite3.Connection,
    episode_id: int,
    model: str,
    video_id: str | None,
    *,
    include_stale: bool = False,
) -> sqlite3.Row | None:
    """The result for this prompt version that was made from the episode's current video,
    else None. 'done' only, unless include_stale. The one place that defines when a
    description is valid (a re-pointed episode id makes the old video's description unusable)."""
    statuses = ("done", "stale") if include_stale else ("done",)
    return conn.execute(
        "SELECT * FROM enrichment WHERE episode_id=? AND model=? AND prompt_version=? "
        f"AND status IN ({', '.join('?' * len(statuses))}) AND video_id=?",
        (episode_id, model, PROMPT_VERSION, *statuses, video_id),
    ).fetchone()


def index_description(
    conn: sqlite3.Connection, episode_id: int, models: tuple[str, ...], video_id: str | None
) -> sqlite3.Row | None:
    """The description the index uses: the first model in `models` (config [index]
    enrich_model, best first) with a valid result, stale ones included."""
    for model in models:
        row = valid_enrichment(conn, episode_id, model, video_id, include_stale=True)
        if row is not None:
            return row
    return None


def mark_stale(conn: sqlite3.Connection, episode_id: int) -> int:
    """The transcript of the episode changed: its descriptions must be made again. No
    commit; the caller owns the transaction. Returns the number of rows marked."""
    cur = conn.execute(
        "UPDATE enrichment SET status='stale' WHERE episode_id=? AND status='done'",
        (episode_id,),
    )
    return cur.rowcount
