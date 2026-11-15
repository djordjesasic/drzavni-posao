"""Content search for the HTTP API and MCP tools: episodes by what happens in them,
episode descriptions and transcript excerpts (phase 1b)."""

import json
import sqlite3
import threading
from typing import Any

import numpy as np

from dp.config import get_settings
from dp.db import active_video_id
from dp.enrich.store import valid_enrichment
from dp.index import embed as embed_client
from dp.index.search import Vectors, search
from dp.search.catalog import youtube_url
from dp.transcribe.speakers import OTHER, SURE_SCORE

MAX_EXCERPT_S = 300  # one excerpt covers at most 5 minutes; ask again for more
_MATCHED = {"summary": "opis", "story": "priča", "imitation": "imitacija", "quote": "replika"}

_lock = threading.Lock()
_cache: dict[tuple[int, int], Vectors] = {}  # at most one entry: the current index


def _vectors(conn: sqlite3.Connection) -> Vectors:
    """Window vectors, loaded once and reloaded when the index changes. Window ids are
    never reused (AUTOINCREMENT), so the row count and the highest id change whenever a
    window is added or removed."""
    n, top = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM windows").fetchone()
    with _lock:
        if (n, top) not in _cache:
            _cache.clear()
            _cache[(n, top)] = Vectors(conn)
        return _cache[(n, top)]


def _embed(texts: list[str]) -> np.ndarray:
    return embed_client.embed(get_settings().embed_url, texts, timeout_s=30)


def indexed_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT w.episode_id) FROM windows w JOIN episodes e ON e.id = w.episode_id "
        "WHERE e.retired = 0 AND e.video_id IS NOT NULL"
    ).fetchone()[0]


def search_content(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Episodes whose transcript or description matches the query, best first, each with
    the minute of the best matching piece and a YouTube link to that minute."""
    hits = search(conn, query, _embed, _vectors(conn), limit=limit)
    if not hits:
        return []
    marks = ",".join("?" * len(hits))
    eps = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, season, title, air_date, video_id FROM episodes WHERE id IN ({marks})",
            [h["episode_id"] for h in hits],
        )
    }
    out = []
    for h in hits:
        ep = eps.get(h["episode_id"])
        if ep is None:
            continue
        t = h["start_s"]
        out.append(
            {
                "episode": ep["id"],
                "title": ep["title"],
                "season": ep["season"],
                "air_date": ep["air_date"],
                "minute": f"{t // 60}:{t % 60:02d}",
                "youtube_url": youtube_url(ep["video_id"], t),
                "matched": _MATCHED.get(h["kind"], "transkript"),
                "snippet": h["snippet"],
                "score": h["score"],
            }
        )
    return out


def description(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any] | None:
    """The episode description used by the index (model from config [index]), or None."""
    vid = active_video_id(conn, episode_id)
    if vid is None:
        return None
    row = valid_enrichment(
        conn, episode_id, get_settings().index_enrich_model, vid, include_stale=True
    )
    if row is None:
        return None
    d = json.loads(row["data"])
    keys = ("summary", "topics", "current_event", "opening_scene", "characters", "stories",
            "imitations", "quotes", "public_figures", "places", "objects")  # fmt: skip
    return {k: d[k] for k in keys if d.get(k)}


def _speaker(conn: sqlite3.Connection, actor: str | None, score: float | None) -> str | None:
    if actor is None or actor == OTHER:
        return None
    row = conn.execute("SELECT name FROM characters WHERE id=?", (actor,)).fetchone()
    name = row[0] if row else actor
    return name if (score or 0) >= SURE_SCORE else f"{name}?"


def transcript_excerpt(
    conn: sqlite3.Connection, episode_id: int, start_s: int = 0, end_s: int | None = None
) -> dict[str, Any] | None:
    """Transcript lines between two times (at most MAX_EXCERPT_S), with the speaker found
    by voice ("?" when unsure); None if the episode is not active or has no transcript."""
    vid = active_video_id(conn, episode_id)
    if vid is None:
        return None
    start_s = max(0, start_s)
    end_s = start_s + MAX_EXCERPT_S if end_s is None else min(end_s, start_s + MAX_EXCERPT_S)
    rows = conn.execute(
        "SELECT start_ms, text, actor, actor_score FROM segments WHERE episode_id=? "
        "AND start_ms >= ? AND start_ms < ? ORDER BY idx",
        (episode_id, start_s * 1000, end_s * 1000),
    ).fetchall()
    has_any = conn.execute(
        "SELECT 1 FROM segments WHERE episode_id=? LIMIT 1", (episode_id,)
    ).fetchone()
    if not has_any:
        return None
    lines = []
    for r in rows:
        t = r["start_ms"] // 1000
        lines.append(
            {
                "time": f"{t // 60}:{t % 60:02d}",
                "speaker": _speaker(conn, r["actor"], r["actor_score"]),
                "text": r["text"],
            }
        )
    return {
        "episode": episode_id,
        "from": f"{start_s // 60}:{start_s % 60:02d}",
        "to": f"{end_s // 60}:{end_s % 60:02d}",
        "youtube_url": youtube_url(vid, start_s),
        "lines": lines,
    }
